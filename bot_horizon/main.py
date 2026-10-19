import asyncio
import contextlib
import logging
import os
import time
from pathlib import Path
from typing import Any, AsyncIterator

import discord
from dotenv import load_dotenv

from . import __version__
from .config import Settings
from .form import (
    OLD_PANEL_IDS,
    PANEL_IDS,
    FormView,
    TaskError,
    panel_ids,
)
from .github_client import GitHubClient
from .notifications import (
    COMMENTED,
    CREATED,
    DELETED,
    DISCORD_TO_GITHUB,
    GITHUB_TO_DISCORD,
    LANGUAGES,
    UPDATED,
    Notification,
    build_notification,
    language_listing,
    language_words,
    notification_text,
    post_card,
    state_changes,
    task_details,
)
from .sync import (
    DISCORD_MARKER,
    DONE_STATUS,
    GITHUB_MARKER,
    PRIORITY_NAMES,
    REOPENED_STATUS,
    STATUS_NAMES,
    discord_comment_body,
    discord_message_body,
    discord_task_data,
    discord_thread_id,
    github_comment_message,
    github_item_comments,
    github_item_data,
    github_item_has_all_comments,
    github_item_has_discord_marker,
    github_item_key,
    github_item_source_url,
    is_bot_comment,
    is_bot_message,
    issue_body,
    same_card,
    task_state,
    thread_task,
)


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

LANGUAGE_COMMAND = "!lang"
# The language chosen from Discord, kept out of the environment so that it
# survives a restart without an edit to `.env`.
LANGUAGE_FILE = Path(".horizon-language")
# The file the settings are read from: `.env`, or `.env.dev` while developing.
ENV_FILE_VARIABLE = "HORIZON_ENV_FILE"
# The choices of the form change rarely: read them again after that.
FORM_OPTIONS_TTL = 10 * 60
# Writes to GitHub not seen back yet, per task: past that, the oldest go.
PENDING_WRITES_LIMIT = 5
NO_MENTIONS = discord.AllowedMentions.none()


def thread_signature(thread: discord.Thread) -> tuple[str, frozenset[str]]:
    """The name and tags of a post, to compare two versions of it."""
    return thread.name, frozenset(tag.name for tag in thread.applied_tags)


def notification_embed(notification: Notification) -> discord.Embed:
    """The card of a notification, as a Discord embed."""
    embed = discord.Embed(
        title=notification.title,
        colour=notification.color,
        url=notification.url or None,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_author(name=notification.author)
    for name, value, inline in notification.fields:
        embed.add_field(name=name, value=value, inline=inline)
    embed.set_footer(text="Horizon")
    return embed


class HorizonBot(discord.Client):
    def __init__(self, settings: Settings, github: GitHubClient) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.settings = settings
        self.github = github
        self.assignee_field_name = settings.github_assignee_field_name
        self.known_github_items: set[str] = set()
        # Every field of each task, as the last poll read it.
        self.known_github_details: dict[str, dict[str, Any]] = {}
        self.known_github_sources: dict[str, str] = {}
        # Comment id → (body, url), per task.
        self.known_github_comments: dict[str, dict[str, tuple[str, str]]] = {}
        # GitHub comment id → the id of the Discord message it was copied to.
        self.relayed_comments: dict[str, int] = {}
        self.threads_deleted_by_bot: set[int] = set()
        self.messages_deleted_by_bot: set[int] = set()
        # The echoes of the bot's own writes, not to be announced again: the
        # post it just edited, and the tasks it just pushed to GitHub.
        self.threads_edited_by_bot: dict[int, tuple[str, frozenset[str]]] = {}
        self.github_writes_from_discord: dict[str, list[dict[str, Any]]] = {}
        self.language = self.stored_language()
        self.poll_task: asyncio.Task[None] | None = None
        self.form_ready = False
        self.cached_form_options: tuple[float, dict[str, Any] | None] = (0.0, None)
        # The shared card of the form channel.
        self.form_view: FormView | None = None

    @property
    def words(self) -> dict[str, str]:
        return language_words(self.language)

    async def setup_hook(self) -> None:
        # The panel buttons outlive the process: they answer again after a restart.
        if self.settings.discord_form_channel_id:
            self.form_view = FormView(self)
            self.add_view(self.form_view)

    async def on_ready(self) -> None:
        logger.info("Connected as %s", self.user)
        if self.poll_task is None:
            self.poll_task = asyncio.create_task(self.poll_github())
        if not self.form_ready:
            self.form_ready = True
            try:
                await self.ensure_form_panel()
            except Exception:
                logger.exception("Could not set up the task form panel")

    def stored_language(self) -> str:
        """The language chosen from Discord, or the one set in the environment."""
        try:
            code = LANGUAGE_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            return self.settings.notification_language
        return code if code in LANGUAGES else self.settings.notification_language

    def store_language(self, code: str) -> None:
        self.language = code
        try:
            LANGUAGE_FILE.write_text(code, encoding="utf-8")
        except OSError:
            logger.exception(
                "Language set to %s for this run, but %s could not be written, so a "
                "restart will fall back to %s",
                code,
                LANGUAGE_FILE,
                self.settings.notification_language,
            )

    def is_notification_channel(self, channel: discord.abc.GuildChannel) -> bool:
        return bool(
            self.settings.discord_notification_channel_id
        ) and channel.id == self.settings.discord_notification_channel_id

    async def handle_language_command(self, message: discord.Message) -> None:
        """`!lang` lists the languages, `!lang <code>` switches the cards to one."""
        content = message.content.strip()
        if content.split(" ")[0].casefold() != LANGUAGE_COMMAND:
            return
        code = content[len(LANGUAGE_COMMAND) :].strip().casefold()
        if not code:
            await message.channel.send(
                f"`{LANGUAGE_COMMAND} <code>`\n{language_listing(self.language)}"
            )
            return
        if code not in LANGUAGES:
            await message.channel.send(
                f"Unknown language `{code}`.\n{language_listing(self.language)}"
            )
            return
        permissions = getattr(message.author, "guild_permissions", None)
        if not getattr(permissions, "manage_guild", False):
            await message.channel.send(
                "Only members who can manage the server may change the language."
            )
            return
        self.store_language(code)
        logger.info("Notification language set to %s by %s", code, message.author)
        # The form card speaks the language of the cards: redraw it in the new one.
        if self.form_view is not None and self.form_view.message is not None:
            try:
                await self.form_view.redraw()
            except Exception:
                logger.exception("Could not redraw the task form in %s", code)
        # The confirmation is the card itself, in the language just chosen.
        await message.channel.send(
            f"\N{WHITE HEAVY CHECK MARK} `{code}` \N{EM DASH} "
            f"{language_words(code)['name']}",
            embed=notification_embed(
                build_notification(
                    CREATED,
                    DISCORD_TO_GITHUB,
                    language_words(code)["untitled"],
                    details={"status": "Backlog", "priority": "Low", "assignee": ""},
                    language=code,
                )
            ),
        )

    # --- Discord → GitHub ---------------------------------------------------

    async def on_message(self, message: discord.Message) -> None:
        if self.is_notification_channel(message.channel) and not is_bot_message(
            message
        ):
            try:
                await self.handle_language_command(message)
            except Exception:
                logger.exception("Could not handle a notification channel command")
            return
        if (
            not self.is_configured_channel(message.channel)
            or is_bot_message(message)
            or DISCORD_MARKER in message.content
        ):
            return
        if self.is_post_reply(message):
            await self.comment_on_github(message)
            return
        if not message.content.strip():
            return

        try:
            task = discord_task_data(message)
            issue_url = await self.github.create_issue(
                title=task["title"],
                body=discord_message_body(message),
                status=task["status"],
                priority=task["priority"],
                assignees=task["assignees"],
                closed=task["closed"],
            )
            logger.info("Created GitHub issue from Discord message: %s", issue_url)
        except Exception:
            logger.exception("Could not create a GitHub issue from Discord message")
            return
        # Link the post to its issue right away: the Project item listing only
        # shows the issue minutes later, and a reply may come before.
        self.known_github_sources[issue_url] = message.jump_url
        self.known_github_details[issue_url] = {**task, "url": issue_url}
        await self.notify(
            CREATED,
            DISCORD_TO_GITHUB,
            task["title"],
            details=task_details(task),
            links={"GitHub": issue_url, "Discord": message.jump_url},
        )

    def is_post(self, channel: Any) -> bool:
        """A post of the task forum."""
        return getattr(channel, "parent_id", None) == self.settings.discord_channel_id

    def is_post_reply(self, message: discord.Message) -> bool:
        """A message posted inside a forum post, after the one that opened it.

        The opening message of a post shares the id of its thread.
        """
        return self.is_post(message.channel) and message.id != message.channel.id

    async def comment_on_github(self, message: discord.Message) -> None:
        """Copy a reply of a Discord post as a comment on the linked issue."""
        if not message.content.strip() and not message.attachments:
            return
        thread = message.channel
        try:
            comment_url = await self.github.comment_from_discord(
                thread.jump_url,
                discord_comment_body(message),
                self.tracked_key_of_thread(thread.id),
            )
        except Exception:
            logger.exception(
                "Could not copy a Discord message to GitHub: %s", message.jump_url
            )
            return
        if not comment_url:
            logger.info(
                "No GitHub issue linked to Discord thread %s, message not copied",
                thread.jump_url,
            )
            return
        logger.info("Copied a Discord message to GitHub: %s", comment_url)
        await self.notify(
            COMMENTED,
            DISCORD_TO_GITHUB,
            thread.name,
            message=(
                message.author.display_name,
                message.clean_content
                or ", ".join(attachment.filename for attachment in message.attachments),
            ),
            links={"GitHub": comment_url, "Discord": message.jump_url},
        )

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        message = payload.message
        if not self.is_post(message.channel) or is_bot_message(message):
            return
        before = payload.cached_message
        # Discord also reports an edit when it unfurls a link: nothing to copy.
        if before is not None and (before.content, before.attachments) == (
            message.content,
            message.attachments,
        ):
            return
        if message.id == message.channel.id:
            await self.sync_opening_message(message)
        else:
            await self.edit_github_comment(message)

    async def sync_opening_message(self, message: discord.Message) -> None:
        """Push the assignees and the description of an edited post to GitHub."""
        thread = message.channel
        task = discord_task_data(message, thread)
        key = self.tracked_key_of_thread(thread.id)
        base = self.latest_details(key)
        if base is not None and (
            base["description"].strip() == task["description"].strip()
            and base["assignee"].casefold() == task["assignee"].casefold()
        ):
            return
        try:
            key = await self.github.update_linked_task(
                thread.jump_url,
                key,
                description=task["description"],
                assignees=task["assignees"],
            )
        except Exception:
            logger.exception(
                "Could not push the edited post to GitHub: %s", thread.jump_url
            )
            return
        logger.info("Updated GitHub issue from edited Discord post: %s", thread.jump_url)
        base = self.latest_details(key)
        if key is None or base is None:
            return
        updated = {
            **base,
            "description": task["description"],
            "assignees": task["assignees"],
            "assignee": task["assignee"],
        }
        self.expect_github(key, updated)
        if changes := state_changes(task_state(base), task_state(updated)):
            await self.notify(
                UPDATED,
                DISCORD_TO_GITHUB,
                thread.name,
                changes=changes,
                links={"Discord": thread.jump_url},
            )

    async def edit_github_comment(self, message: discord.Message) -> None:
        thread = message.channel
        try:
            comment_url = await self.github.edit_comment_from_discord(
                thread.jump_url,
                message.jump_url,
                discord_comment_body(message),
                self.tracked_key_of_thread(thread.id),
            )
        except Exception:
            logger.exception(
                "Could not copy an edited Discord message to GitHub: %s",
                message.jump_url,
            )
            return
        if comment_url:
            logger.info("Edited the GitHub comment of a Discord message: %s", comment_url)

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if payload.message_id in self.messages_deleted_by_bot:
            self.messages_deleted_by_bot.discard(payload.message_id)
            return
        cached = payload.cached_message
        # The opening message goes with its post, which on_thread_delete handles.
        if (cached is not None and is_bot_message(cached)) or (
            payload.message_id == payload.channel_id
        ):
            return
        thread = self.get_channel(payload.channel_id)
        if thread is None or not self.is_post(thread):
            return
        message_url = (
            f"https://discord.com/channels/{payload.guild_id}/"
            f"{payload.channel_id}/{payload.message_id}"
        )
        try:
            comment_url = await self.github.delete_comment_from_discord(
                thread.jump_url, message_url, self.tracked_key_of_thread(thread.id)
            )
        except Exception:
            logger.exception(
                "Could not delete the GitHub comment of a Discord message: %s",
                message_url,
            )
            return
        if comment_url:
            logger.info("Deleted the GitHub comment of a Discord message: %s", comment_url)

    async def on_thread_update(
        self, before: discord.Thread, after: discord.Thread
    ) -> None:
        if not self.is_configured_channel(after):
            return
        # The echo of the edit the bot just made on this post.
        if self.threads_edited_by_bot.pop(after.id, None) == thread_signature(after):
            return
        old, new = thread_task(before), thread_task(after)
        changes = state_changes(tuple(old.values()), tuple(new.values()))
        # Discord also emits this event for an archive, a slow mode or a
        # pin. No tracked field moves then, and GitHub already carries those
        # values: neither a write nor an announcement.
        if not changes:
            return
        # The Done tag and a closed issue go together.
        closed = None
        if new["status"] != old["status"]:
            if new["status"] == DONE_STATUS:
                closed = True
            elif old["status"] == DONE_STATUS:
                closed = False
        try:
            key = await self.github.update_issue_from_discord(
                after.jump_url,
                new["title"],
                new["status"],
                new["priority"],
                previous_title=before.name,
                item_key=self.tracked_key_of_thread(after.id),
                closed=closed,
            )
            logger.info("Updated GitHub issue from Discord thread: %s", after.jump_url)
        except Exception:
            logger.exception("Could not update GitHub issue from Discord thread")
            return
        if key and (base := self.latest_details(key)) is not None:
            updated = {**base, **new}
            if closed is not None:
                updated["closed"] = closed
            # The poll will see this change come back from GitHub: it is ours.
            self.expect_github(key, updated)
        if closed:
            await self.set_archived(after, True)
        await self.notify(
            UPDATED,
            DISCORD_TO_GITHUB,
            new["title"],
            changes=changes,
            links={"Discord": after.jump_url},
        )

    async def on_thread_delete(self, thread: discord.Thread) -> None:
        if not self.is_configured_channel(thread):
            return
        if thread.id in self.threads_deleted_by_bot:
            self.threads_deleted_by_bot.discard(thread.id)
            return
        try:
            key = await self.github.delete_task_from_discord(
                thread.jump_url, self.tracked_key_of_thread(thread.id), thread.name
            )
        except Exception:
            logger.exception("Could not delete GitHub task from Discord thread")
            return
        if key:
            self.forget_github_item(key)
            logger.info("Deleted GitHub task of Discord thread: %s", thread.jump_url)
            await self.notify(
                DELETED,
                DISCORD_TO_GITHUB,
                thread.name,
                links={"Discord": thread.jump_url},
            )
        else:
            logger.info(
                "No GitHub task linked to deleted Discord thread: %s", thread.jump_url
            )

    # --- what the bot knows -------------------------------------------------

    def tracked_key_of_thread(self, thread_id: int) -> str | None:
        """Find a known GitHub item from the Discord thread it was published to."""
        return next(
            (
                key
                for key, source_url in self.known_github_sources.items()
                if discord_thread_id(source_url) == thread_id
            ),
            None,
        )

    def forget_github_item(self, key: str) -> None:
        self.known_github_items.discard(key)
        self.known_github_details.pop(key, None)
        self.known_github_sources.pop(key, None)
        self.known_github_comments.pop(key, None)
        self.github_writes_from_discord.pop(key, None)

    def latest_details(self, key: str | None) -> dict[str, Any] | None:
        """A task as it stands after the writes the bot is still waiting to see."""
        if key is None:
            return None
        pending = self.github_writes_from_discord.get(key)
        return pending[-1] if pending else self.known_github_details.get(key)

    def expect_github(self, key: str, details: dict[str, Any]) -> None:
        """Remember a write to GitHub, so that the poll recognises it coming back."""
        pending = self.github_writes_from_discord.setdefault(key, [])
        pending.append(details)
        del pending[:-PENDING_WRITES_LIMIT]

    def is_own_write(self, key: str, details: dict[str, Any]) -> bool:
        """Whether GitHub now shows one of the writes the bot made from Discord.

        The Project listing lags behind: a change read now may be the first of
        several the bot made, so every write until that one is settled.
        """
        pending = self.github_writes_from_discord.get(key, [])
        for index, written in enumerate(pending):
            if same_card(written, details):
                del pending[: index + 1]
                if not pending:
                    self.github_writes_from_discord.pop(key, None)
                return True
        return False

    def item_details(self, item: dict[str, object]) -> dict[str, Any]:
        settings = self.settings
        return github_item_data(
            item,
            settings.github_assignee_field_name,
            settings.github_priority_field_name,
            settings.github_size_field_name,
            settings.github_start_date_field_name,
            settings.github_target_date_field_name,
        )

    def github_title(self, item: dict[str, object]) -> str:
        """The task title, truncated the way a Discord post name is."""
        return self.item_details(item)["title"][:100]

    async def notify(
        self,
        action: str,
        direction: str,
        title: str,
        *,
        details: dict[str, str] | None = None,
        changes: list[str] | None = None,
        message: tuple[str, str] | None = None,
        links: dict[str, str] | None = None,
    ) -> None:
        """Announce an action in the notification channel, when one is set.

        A notification that fails is logged without interrupting the
        synchronisation, which is already done by then.
        """
        channel_id = self.settings.discord_notification_channel_id
        if not channel_id:
            return
        notification = build_notification(
            action,
            direction,
            title,
            details=details,
            changes=changes,
            message=message,
            links=links,
            language=self.language,
        )
        try:
            channel = self.get_channel(channel_id) or await self.fetch_channel(
                channel_id
            )
            if not isinstance(channel, discord.abc.Messageable):
                raise RuntimeError(
                    "DISCORD_NOTIFICATION_CHANNEL_ID must point to a channel the bot "
                    "can post in"
                )
            try:
                await channel.send(embed=notification_embed(notification))
            except discord.Forbidden:
                # Without the Embed Links permission the embed is refused,
                # but plain text still goes through.
                await channel.send(notification_text(notification))
        except Exception:
            logger.exception("Could not send the notification: %s", title)

    def is_configured_channel(self, channel: discord.abc.GuildChannel) -> bool:
        return channel.id == self.settings.discord_channel_id or getattr(
            channel, "parent_id", None
        ) == self.settings.discord_channel_id

    # --- the forum posts ----------------------------------------------------

    def forum_tags(
        self, forum: discord.ForumChannel, details: dict[str, Any]
    ) -> list[discord.ForumTag]:
        """The tags of the forum matching the status and the priority of a task."""
        wanted = {details["status"].casefold(), details["priority"].casefold()}
        return [tag for tag in forum.available_tags if tag.name.casefold() in wanted]

    def owns_post(self, thread: discord.Thread) -> bool:
        """A post the bot opened, whose opening message it can rewrite."""
        return self.user is not None and getattr(thread, "owner_id", None) == self.user.id

    async def publish_to_discord(
        self, channel: discord.abc.GuildChannel, item: dict[str, Any]
    ) -> discord.Thread | None:
        content = post_card(item, self.language)
        if isinstance(channel, discord.ForumChannel):
            tags = self.forum_tags(channel, item)
            if not tags:
                raise RuntimeError(
                    "No matching Discord forum tags found for GitHub item"
                )
            thread, _ = await channel.create_thread(
                name=item["title"][:100],
                content=content,
                applied_tags=tags,
                allowed_mentions=NO_MENTIONS,
            )
            if item.get("closed"):
                await self.set_archived(thread, True)
            return thread
        if isinstance(channel, discord.TextChannel):
            await channel.send(content, allowed_mentions=NO_MENTIONS)
            return None
        raise RuntimeError("DISCORD_CHANNEL_ID must point to a text or forum channel")

    async def resolve_thread(self, source_url: str | None) -> discord.Thread | None:
        thread_id = discord_thread_id(source_url)
        if thread_id is None:
            return None
        thread = self.get_channel(thread_id)
        if thread is None:
            try:
                thread = await self.fetch_channel(thread_id)
            except discord.HTTPException:
                return None
        return thread if isinstance(thread, discord.Thread) else None

    async def set_archived(self, thread: discord.Thread, archived: bool) -> None:
        """Archive a post, or bring it back; an archive change is never synchronised."""
        if getattr(thread, "archived", False) == archived:
            return
        try:
            await thread.edit(archived=archived)
        except Exception:
            logger.exception("Could not change the archive state of %s", thread.jump_url)

    @contextlib.asynccontextmanager
    async def unarchived(self, thread: discord.Thread) -> AsyncIterator[None]:
        """An archived post cannot be edited: open it for the time of an edit."""
        archived = getattr(thread, "archived", False)
        if archived:
            await thread.edit(archived=False)
        try:
            yield
        finally:
            if archived:
                await thread.edit(archived=True)

    async def starter_message(self, thread: discord.Thread) -> discord.Message | None:
        try:
            return thread.starter_message or await thread.fetch_message(thread.id)
        except discord.HTTPException:
            return None

    async def refresh_post(
        self,
        source_url: str | None,
        details: dict[str, Any],
        archive: bool | None = None,
    ) -> None:
        """Bring a post in line with its task: name, tags, opening message.

        `archive` archives the post or brings it back; None keeps it as it is.
        """
        thread = await self.resolve_thread(source_url)
        if thread is not None:
            await self.refresh_thread(thread, details, archive)

    async def refresh_thread(
        self,
        thread: discord.Thread,
        details: dict[str, Any],
        archive: bool | None = None,
    ) -> None:
        if not isinstance(thread.parent, discord.ForumChannel):
            return
        tags = self.forum_tags(thread.parent, details)
        name = details["title"][:100].strip()
        signature = (name, frozenset(tag.name for tag in tags))
        # A post already up to date is not edited: Discord would emit an
        # update event for nothing.
        rename = thread_signature(thread) != signature
        starter = card = None
        if self.owns_post(thread):
            starter = await self.starter_message(thread)
            card = post_card(details, self.language)
            if starter is None or starter.content == card:
                starter = None
        archived = getattr(thread, "archived", False)
        target = archived if archive is None else archive
        if not rename and starter is None and archived == target:
            return
        edits: dict[str, Any] = {}
        if rename:
            edits.update(name=name, applied_tags=tags)
            self.threads_edited_by_bot[thread.id] = signature
        if archived:
            edits["archived"] = False
        try:
            if edits:
                await thread.edit(**edits)
            if starter is not None:
                await starter.edit(content=card, allowed_mentions=NO_MENTIONS)
            if target:
                await thread.edit(archived=True)
        except Exception:
            # No edit means no echo to ignore: do not swallow the next
            # change made by a user.
            if rename:
                self.threads_edited_by_bot.pop(thread.id, None)
            raise

    async def delete_discord_thread(self, source_url: str | None) -> None:
        thread = await self.resolve_thread(source_url)
        if thread is None:
            return
        self.threads_deleted_by_bot.add(thread.id)
        try:
            await thread.delete()
            logger.info("Deleted Discord thread of removed GitHub task: %s", source_url)
        except Exception:
            self.threads_deleted_by_bot.discard(thread.id)
            logger.exception("Could not delete Discord thread: %s", source_url)

    async def link_github_item(
        self, key: str, item: dict[str, object], thread: discord.Thread
    ) -> None:
        """Remember the Discord thread of a published task, in memory and on GitHub."""
        self.known_github_sources[key] = thread.jump_url
        try:
            await self.github.set_discord_source(item, thread.jump_url)
        except Exception:
            logger.exception(
                "Published %s to Discord but could not write the source link back, "
                "so deleting that thread will not delete the task after a restart",
                thread.jump_url,
            )

    async def forum_threads(
        self, channel: discord.ForumChannel
    ) -> tuple[list[discord.Thread], bool]:
        """Every post of the forum, including the archived ones.

        The flag says whether the archived posts could be listed: without
        them, a task may look as if it had no post when it has one.
        """
        threads = list(channel.threads)
        try:
            async for thread in channel.archived_threads(limit=None):
                threads.append(thread)
        except discord.HTTPException:
            logger.warning(
                "Could not list the archived posts of %s, tasks linked to an archived "
                "post may stay unlinked",
                channel.name,
                exc_info=True,
            )
            return threads, False
        return threads, True

    async def reconcile_links(
        self, channel: discord.abc.GuildChannel, items: dict[str, dict[str, object]]
    ) -> set[str]:
        """Rebuild the missing links by matching a task title with a post name.

        The bot names a post after its task title and keeps both in sync, so an
        exact match identifies the pair. Tasks published before the source line
        was written back to GitHub are repaired this way, without any manual step.

        Returns the tasks no post matches at all: tasks the bot never published,
        such as the ones past the 100 items version 1 read, or the ones that
        existed before its first start.
        """
        unlinked = {
            key: item
            for key, item in items.items()
            if not github_item_source_url(item) and key not in self.known_github_sources
        }
        if not unlinked or not isinstance(channel, discord.ForumChannel):
            return set()
        threads, complete = await self.forum_threads(channel)
        posts: dict[str, list[discord.Thread]] = {}
        for thread in threads:
            posts.setdefault(thread.name.casefold(), []).append(thread)
        missing: set[str] = set()
        linked_thread_ids = {
            thread_id
            for source_url in self.known_github_sources.values()
            if (thread_id := discord_thread_id(source_url)) is not None
        }
        for key, item in unlinked.items():
            title = self.github_title(item)
            candidates = [
                thread
                for thread in posts.get(title.casefold(), [])
                if thread.id not in linked_thread_ids
            ]
            # Only a full listing proves that a task has no post: a missing
            # archive would publish its posts a second time.
            if not candidates and complete and not github_item_has_discord_marker(item):
                missing.add(key)
                continue
            if len(candidates) != 1:
                logger.info(
                    "Leaving GitHub task %r unlinked: %d Discord posts match its title",
                    title,
                    len(candidates),
                )
                continue
            await self.link_github_item(key, item, candidates[0])
            linked_thread_ids.add(candidates[0].id)
            logger.info(
                "Linked GitHub task %r to existing Discord post %s",
                title,
                candidates[0].jump_url,
            )
        return missing

    # --- GitHub → Discord ---------------------------------------------------

    async def poll_github(self) -> None:
        await self.wait_until_ready()
        channel = self.get_channel(self.settings.discord_channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
            raise RuntimeError("DISCORD_CHANNEL_ID must point to a text or forum channel")

        while not self.is_closed():
            try:
                await self.poll_once(channel)
            except Exception:
                logger.exception("GitHub polling failed")
            await asyncio.sleep(self.settings.github_poll_interval_seconds)

    async def poll_once(self, channel: discord.abc.GuildChannel) -> None:
        items = {
            key: item
            for item in await self.github.list_items()
            if (key := github_item_key(item))
        }
        # The first poll takes GitHub as it is as the baseline: announcing it
        # would republish every task at each restart.
        baseline = not self.known_github_items
        missing: set[str] = set()
        if baseline:
            missing = await self.reconcile_links(channel, items)
            if missing:
                logger.info(
                    "Publishing %d GitHub tasks that have no Discord post yet",
                    len(missing),
                )
        for key, item in items.items():
            details = self.item_details(item)
            if source_url := github_item_source_url(item):
                self.known_github_sources[key] = source_url
            if baseline:
                self.known_github_details[key] = details
                if key in missing:
                    await self.catch_up(channel, key, item, details)
            elif key not in self.known_github_items:
                self.known_github_details[key] = details
                if not github_item_has_discord_marker(item) and not source_url:
                    await self.publish_github_task(channel, key, item, details)
            else:
                await self.apply_github_change(key, item, details)
            await self.relay_github_comments(key, item, baseline=baseline)
        if not baseline:
            for key in self.known_github_items - items.keys():
                source_url = self.known_github_sources.get(key)
                title = (self.known_github_details.get(key) or {}).get("title", "")
                await self.delete_discord_thread(source_url)
                self.forget_github_item(key)
                await self.notify(
                    DELETED,
                    GITHUB_TO_DISCORD,
                    title,
                    links={
                        # An item key is its issue url, except for
                        # a draft item, which has none.
                        "GitHub": key if key.startswith("http") else "",
                        "Discord": source_url or "",
                    },
                )
        self.known_github_items = set(items)

    async def catch_up(
        self,
        channel: discord.abc.GuildChannel,
        key: str,
        item: dict[str, object],
        details: dict[str, Any],
    ) -> None:
        """Publish a task that never had a post, without announcing it.

        A task that cannot be published is logged and left for the next start,
        rather than stopping the others.
        """
        try:
            await self.publish_github_task(channel, key, item, details, announce=False)
            logger.info("Published missing Discord post for GitHub task %r", details["title"])
        except Exception:
            logger.exception("Could not publish GitHub task %r", details["title"])

    async def publish_github_task(
        self,
        channel: discord.abc.GuildChannel,
        key: str,
        item: dict[str, object],
        details: dict[str, Any],
        announce: bool = True,
    ) -> None:
        thread = await self.publish_to_discord(channel, details)
        if thread is not None:
            await self.link_github_item(key, item, thread)
        if not announce:
            return
        await self.notify(
            CREATED,
            GITHUB_TO_DISCORD,
            details["title"],
            details=task_details(details),
            links={
                "GitHub": details["url"],
                "Discord": thread.jump_url if thread else "",
            },
        )

    async def apply_github_change(
        self, key: str, item: dict[str, object], details: dict[str, Any]
    ) -> None:
        """Bring the post of a task in line with a change made on GitHub."""
        known = self.known_github_details.get(key)
        if known is None:
            self.known_github_details[key] = details
            return
        if same_card(known, details):
            return
        # A change made in Discord comes back here on a later poll: it is
        # already announced, and the post already carries its values.
        if self.is_own_write(key, details):
            self.known_github_details[key] = details
            return
        closing = details["closed"] and not known["closed"]
        reopening = known["closed"] and not details["closed"]
        status = details["status"]
        if closing and status != DONE_STATUS:
            status = DONE_STATUS
        elif reopening and status == DONE_STATUS:
            status = REOPENED_STATUS
        if status != details["status"]:
            await self.github.update_project_status(item["id"], status)
            details = {**details, "status": status}
        changes = state_changes(task_state(known), task_state(details))
        self.known_github_details[key] = details
        await self.refresh_post(
            github_item_source_url(item) or self.known_github_sources.get(key),
            details,
            archive=True if closing else False if reopening else None,
        )
        if changes:
            await self.notify(
                UPDATED,
                GITHUB_TO_DISCORD,
                details["title"],
                changes=changes,
                links={
                    "GitHub": details["url"],
                    "Discord": self.known_github_sources.get(key, ""),
                },
            )

    async def relay_github_comments(
        self, key: str, item: dict[str, object], *, baseline: bool
    ) -> None:
        """Mirror in the Discord post of a task what happened to its GitHub comments.

        New comments are posted, edited ones rewritten and deleted ones removed.
        The first poll only records the comments already there, so that a
        restart does not post a whole conversation again. The comments the bot
        wrote from Discord carry a marker and never come back.
        """
        comments = github_item_comments(item)
        current = {comment["id"]: comment for comment in comments}
        seen = self.known_github_comments.get(key, {})
        self.known_github_comments[key] = {
            comment["id"]: (comment["body"], comment["url"]) for comment in comments
        }
        if baseline:
            return
        new_comments = [
            comment
            for comment in comments
            if comment["id"] not in seen and not is_bot_comment(comment)
        ]
        edited = [
            comment
            for comment in comments
            if comment["id"] in seen
            and seen[comment["id"]][0] != comment["body"]
            and not is_bot_comment(comment)
        ]
        # Past 100 comments the oldest leave the listing: their absence is not
        # a deletion.
        deleted = [
            (comment_id, url)
            for comment_id, (body, url) in seen.items()
            if comment_id not in current and GITHUB_MARKER not in body
        ] if github_item_has_all_comments(item) else []
        if not (new_comments or edited or deleted):
            return
        thread = await self.resolve_thread(
            github_item_source_url(item) or self.known_github_sources.get(key)
        )
        if thread is None:
            return
        for comment in new_comments:
            try:
                sent = await thread.send(
                    github_comment_message(comment), allowed_mentions=NO_MENTIONS
                )
                logger.info("Copied a GitHub comment to Discord: %s", comment["url"])
            except Exception:
                logger.exception(
                    "Could not copy a GitHub comment to Discord: %s", comment["url"]
                )
                continue
            self.relayed_comments[comment["id"]] = sent.id
            await self.notify(
                COMMENTED,
                GITHUB_TO_DISCORD,
                self.github_title(item),
                message=(comment["author"], comment["body"]),
                links={"GitHub": comment["url"], "Discord": sent.jump_url},
            )
        for comment in edited:
            await self.change_relayed_message(thread, comment["id"], comment["url"], comment)
        for comment_id, url in deleted:
            await self.change_relayed_message(thread, comment_id, url, None)

    async def change_relayed_message(
        self,
        thread: discord.Thread,
        comment_id: str,
        url: str,
        comment: dict[str, str] | None,
    ) -> None:
        """Rewrite the Discord copy of a GitHub comment, or delete it with None."""
        try:
            message = await self.relayed_message(thread, comment_id, url)
            if message is None:
                return
            async with self.unarchived(thread):
                if comment is None:
                    self.messages_deleted_by_bot.add(message.id)
                    await message.delete()
                    self.relayed_comments.pop(comment_id, None)
                else:
                    await message.edit(
                        content=github_comment_message(comment),
                        allowed_mentions=NO_MENTIONS,
                    )
            logger.info(
                "%s the Discord copy of a GitHub comment: %s",
                "Deleted" if comment is None else "Edited",
                url,
            )
        except Exception:
            logger.exception("Could not change the Discord copy of %s", url)

    async def relayed_message(
        self, thread: discord.Thread, comment_id: str, url: str
    ) -> discord.PartialMessage | discord.Message | None:
        """The Discord message a GitHub comment was copied to.

        The bot remembers it while it runs; after a restart, the copy is found
        by the comment link it ends with.
        """
        if (message_id := self.relayed_comments.get(comment_id)) is not None:
            return thread.get_partial_message(message_id)
        async for message in thread.history(limit=200):
            if is_bot_message(message) and f"<{url}>" in message.content:
                return message
        return None

    # --- the task form ------------------------------------------------------

    async def ensure_form_panel(self) -> None:
        """Show the form card in its channel, as an empty new task.

        The card of a previous run is redrawn in place; the panels of earlier
        versions, whose buttons nothing answers any more, are deleted.
        """
        view = self.form_view
        channel_id = self.settings.discord_form_channel_id
        if not channel_id or view is None:
            return
        channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            logger.warning("DISCORD_FORM_CHANNEL_ID must point to a text channel")
            return
        view.options = await self.form_options()
        view.reset()
        card = None
        async for message in channel.history(limit=50):
            if message.author != self.user:
                continue
            ids = panel_ids(message)
            if card is None and ids & PANEL_IDS:
                card = message
            elif ids & (PANEL_IDS | OLD_PANEL_IDS):
                await message.delete()
        if card is None:
            view.message = await channel.send(view=view)
            logger.info("Posted the task form in #%s", channel.name)
        else:
            view.message = card
            await view.redraw()

    async def form_options(self) -> dict[str, Any]:
        """What the form offers: the values of each field, and which fields exist.

        Read from GitHub at most every 10 minutes. A failed read keeps the
        previous choices, or offers none of the optional ones.
        """
        read_at, options = self.cached_form_options
        if options is not None and time.monotonic() - read_at < FORM_OPTIONS_TTL:
            return options
        settings = self.settings
        try:
            size_field = await self.github.project_field(settings.github_size_field_name)
            options = {
                "statuses": list(STATUS_NAMES),
                "priorities": list(PRIORITY_NAMES),
                "sizes": [
                    option["name"] for option in (size_field or {}).get("options", [])
                ],
                "assignees": await self.github.assignable_users(),
                "start_date": bool(
                    await self.github.project_field(settings.github_start_date_field_name)
                ),
                "target_date": bool(
                    await self.github.project_field(settings.github_target_date_field_name)
                ),
            }
        except Exception:
            logger.warning("Could not read the choices of the task form", exc_info=True)
            if options is not None:
                return options
            return {
                "statuses": list(STATUS_NAMES),
                "priorities": list(PRIORITY_NAMES),
                "sizes": [],
                "assignees": [],
                "start_date": False,
                "target_date": False,
            }
        self.cached_form_options = (time.monotonic(), options)
        return options

    async def create_task_from_form(self, draft: dict[str, Any]) -> discord.Thread:
        """Open the post of a new task, then its issue, and link the two."""
        forum = self.get_channel(self.settings.discord_channel_id)
        if not isinstance(forum, discord.ForumChannel):
            raise RuntimeError("DISCORD_CHANNEL_ID must point to a forum channel")
        details = {
            **draft,
            "assignee": ", ".join(draft["assignees"]),
            "url": "",
            "closed": draft["status"] == DONE_STATUS,
        }
        thread, starter = await forum.create_thread(
            name=details["title"][:100],
            content=post_card(details, self.language),
            applied_tags=self.forum_tags(forum, details),
            allowed_mentions=NO_MENTIONS,
        )
        try:
            issue_url = await self.github.create_issue(
                details["title"],
                issue_body(details["description"], thread.jump_url),
                details["status"],
                details["priority"],
                details["assignees"],
                size=details["size"],
                start_date=details["start_date"],
                target_date=details["target_date"],
                closed=details["closed"],
            )
        except Exception:
            # A post without its issue would only confuse: take it back.
            self.threads_deleted_by_bot.add(thread.id)
            try:
                await thread.delete()
            except Exception:
                self.threads_deleted_by_bot.discard(thread.id)
                logger.exception("Could not delete the post of a failed task")
            raise
        logger.info("Created GitHub issue from the task form: %s", issue_url)
        details["url"] = issue_url
        self.known_github_sources[issue_url] = thread.jump_url
        self.known_github_details[issue_url] = details
        try:
            await starter.edit(content=post_card(details, self.language))
        except Exception:
            logger.exception("Could not add the GitHub link to %s", thread.jump_url)
        if details["closed"]:
            await self.set_archived(thread, True)
        await self.notify(
            CREATED,
            DISCORD_TO_GITHUB,
            details["title"],
            details=task_details(details),
            links={"GitHub": issue_url, "Discord": thread.jump_url},
        )
        return thread

    async def load_task_for_form(
        self, thread: discord.Thread
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        """The task of a post, read from GitHub: its key, its item and its fields."""
        if not self.is_post(thread):
            raise TaskError("not_a_task")
        item = await self.github.find_linked_item(
            thread.jump_url, self.tracked_key_of_thread(thread.id), title=thread.name
        )
        if item is None or not (item.get("content") or {}).get("url"):
            raise TaskError("task_not_found")
        return github_item_key(item), item, self.item_details(item)

    async def save_task_from_form(
        self,
        thread: discord.Thread,
        key: str,
        item: dict[str, Any],
        loaded: dict[str, Any],
        draft: dict[str, Any],
    ) -> bool:
        """Write what the form changed, on GitHub and on the post at once."""
        fields = {
            field: draft[field]
            for field in (
                "title",
                "description",
                "status",
                "priority",
                "size",
                "start_date",
                "target_date",
            )
            if (draft[field] or "").strip() != (loaded.get(field) or "").strip()
        }
        if [login.casefold() for login in draft["assignees"]] != [
            login.casefold() for login in loaded["assignees"]
        ]:
            fields["assignees"] = list(draft["assignees"])
        if not fields:
            return False
        closed = None
        if draft["status"] == DONE_STATUS and not loaded["closed"]:
            closed = True
        elif draft["status"] != DONE_STATUS and loaded["closed"]:
            closed = False
        await self.github.update_task(item, **fields, closed=closed)
        logger.info("Updated GitHub issue from the task form: %s", loaded["url"])
        updated = {
            **loaded,
            **fields,
            "assignees": list(draft["assignees"]),
            "assignee": ", ".join(draft["assignees"]),
        }
        if closed is not None:
            updated["closed"] = closed
        self.expect_github(key, updated)
        self.known_github_sources.setdefault(key, thread.jump_url)
        await self.refresh_thread(thread, updated, archive=closed)
        if changes := state_changes(task_state(loaded), task_state(updated)):
            await self.notify(
                UPDATED,
                DISCORD_TO_GITHUB,
                updated["title"],
                changes=changes,
                links={"GitHub": updated["url"], "Discord": thread.jump_url},
            )
        return True

    async def close(self) -> None:
        if self.poll_task:
            self.poll_task.cancel()
        await self.github.close()
        await super().close()


async def run() -> None:
    env_file = os.getenv(ENV_FILE_VARIABLE, ".env")
    load_dotenv(env_file)
    logger.info("Horizon bot %s, settings read from %s", __version__, env_file)
    settings = Settings.from_environment()
    github = GitHubClient(
        settings.github_token,
        settings.github_project_id,
        settings.github_repository_id,
        settings.github_priority_field_name,
        settings.github_assignee_field_name,
        settings.github_org_name,
        settings.github_project_number,
        settings.github_size_field_name,
        settings.github_start_date_field_name,
        settings.github_target_date_field_name,
    )
    bot = HorizonBot(settings, github)
    await bot.start(settings.discord_token)


if __name__ == "__main__":
    asyncio.run(run())
