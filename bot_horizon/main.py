import asyncio
import logging
from pathlib import Path

import discord
from dotenv import load_dotenv

from .config import Settings
from .github_client import GitHubClient
from .notifications import (
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
    state_changes,
    task_details,
)
from .sync import (
    DISCORD_MARKER,
    discord_message_body,
    discord_task_data,
    discord_thread_id,
    github_item_data,
    github_item_has_discord_marker,
    github_item_key,
    github_item_source_url,
    is_bot_message,
    task_state,
)


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

LANGUAGE_COMMAND = "!lang"
# The language chosen from Discord, kept out of the environment so that it
# survives a restart without an edit to `.env`.
LANGUAGE_FILE = Path(".horizon-language")


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
        self.known_github_states: dict[str, tuple[str, ...]] = {}
        self.known_github_sources: dict[str, str] = {}
        self.threads_deleted_by_bot: set[int] = set()
        # The echoes of the bot's own writes, not to be announced again: the
        # post it just edited, and the task it just pushed to GitHub.
        self.threads_edited_by_bot: dict[int, tuple[str, frozenset[str]]] = {}
        self.github_writes_from_discord: dict[str, tuple[str, str, str]] = {}
        self.language = self.stored_language()
        self.poll_task: asyncio.Task[None] | None = None

    async def on_ready(self) -> None:
        logger.info("Connected as %s", self.user)
        if self.poll_task is None:
            self.poll_task = asyncio.create_task(self.poll_github())

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
            or not message.content.strip()
            or DISCORD_MARKER in message.content
        ):
            return

        try:
            task = discord_task_data(message)
            issue_url = await self.github.create_issue(
                title=task["title"],
                body=discord_message_body(message),
                status=task["status"],
                priority=task["priority"],
                assignees=task["assignees"],
            )
            logger.info("Created GitHub issue from Discord message: %s", issue_url)
        except Exception:
            logger.exception("Could not create a GitHub issue from Discord message")
            return
        await self.notify(
            CREATED,
            DISCORD_TO_GITHUB,
            task["title"],
            details=task_details(task),
            links={"GitHub": issue_url, "Discord": message.jump_url},
        )

    async def on_thread_update(
        self, before: discord.Thread, after: discord.Thread
    ) -> None:
        if not self.is_configured_channel(after):
            return
        # The echo of the edit the bot just made on this post.
        if self.threads_edited_by_bot.pop(after.id, None) == thread_signature(after):
            return
        try:
            starter_message = await after.fetch_message(after.id)
            if is_bot_message(starter_message):
                return
            task = discord_task_data(starter_message, after)
            changes = state_changes(
                task_state(discord_task_data(starter_message, before)),
                task_state(task),
            )
            # Discord also emits this event for an archive, a slow mode or a
            # pin. No tracked field moves then, and GitHub already carries those
            # values: neither a write nor an announcement.
            if not changes:
                return
            key = await self.github.update_issue_from_discord(
                after.jump_url,
                task["title"],
                task["status"],
                task["priority"],
                previous_title=before.name,
            )
            logger.info("Updated GitHub issue from Discord thread: %s", after.jump_url)
        except Exception:
            logger.exception("Could not update GitHub issue from Discord thread")
            return
        if key:
            # The poll will see this change come back from GitHub: it is ours.
            self.github_writes_from_discord[key] = (
                task["title"],
                task["status"],
                task["priority"],
            )
        await self.notify(
            UPDATED,
            DISCORD_TO_GITHUB,
            task["title"],
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
        self.known_github_states.pop(key, None)
        self.known_github_sources.pop(key, None)
        self.github_writes_from_discord.pop(key, None)

    async def notify(
        self,
        action: str,
        direction: str,
        title: str,
        *,
        details: dict[str, str] | None = None,
        changes: list[str] | None = None,
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

    async def publish_to_discord(
        self, channel: discord.abc.GuildChannel, item: dict[str, str]
    ) -> discord.Thread | None:
        assignee = item["assignee"] or "Unassigned"
        description = item["description"] or "No description"
        content = (
            f"Assigned to: {assignee}\n"
            f"Description:\n{description}\n"
            f"Source: {item['url']}"
        )
        if isinstance(channel, discord.ForumChannel):
            tags = [
                tag
                for tag in channel.available_tags
                if tag.name.casefold()
                in {item["status"].casefold(), item["priority"].casefold()}
            ]
            if not tags:
                raise RuntimeError(
                    "No matching Discord forum tags found for GitHub item"
                )
            thread, _ = await channel.create_thread(
                name=item["title"][:100], content=content, applied_tags=tags
            )
            return thread
        if isinstance(channel, discord.TextChannel):
            await channel.send(content)
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

    async def forum_threads(self, channel: discord.ForumChannel) -> list[discord.Thread]:
        """Every post of the forum, including the archived ones."""
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
        return threads

    async def reconcile_links(
        self, channel: discord.abc.GuildChannel, items: dict[str, dict[str, object]]
    ) -> None:
        """Rebuild the missing links by matching a task title with a post name.

        The bot names a post after its task title and keeps both in sync, so an
        exact match identifies the pair. Tasks published before the source line
        was written back to GitHub are repaired this way, without any manual step.
        """
        unlinked = {
            key: item
            for key, item in items.items()
            if not github_item_source_url(item) and key not in self.known_github_sources
        }
        if not unlinked or not isinstance(channel, discord.ForumChannel):
            return
        posts: dict[str, list[discord.Thread]] = {}
        for thread in await self.forum_threads(channel):
            posts.setdefault(thread.name.casefold(), []).append(thread)
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

    def github_title(self, item: dict[str, object]) -> str:
        """The task title, truncated the way a Discord post name is."""
        details = github_item_data(
            item,
            self.assignee_field_name,
            self.settings.github_priority_field_name,
        )
        return details["title"][:100]

    async def update_discord_from_github(self, item: dict[str, object]) -> None:
        thread = await self.resolve_thread(github_item_source_url(item))
        if thread is None or not isinstance(thread.parent, discord.ForumChannel):
            return
        details = github_item_data(
            item,
            self.assignee_field_name,
            self.settings.github_priority_field_name,
        )
        tags = [
            tag
            for tag in thread.parent.available_tags
            if tag.name.casefold()
            in {details["status"].casefold(), details["priority"].casefold()}
        ]
        name = details["title"][:100]
        signature = (name, frozenset(tag.name for tag in tags))
        # A post already up to date is not edited: Discord would emit an
        # update event for nothing.
        if thread_signature(thread) == signature:
            return
        self.threads_edited_by_bot[thread.id] = signature
        try:
            await thread.edit(name=name, applied_tags=tags)
        except Exception:
            # No edit means no echo to ignore: do not swallow the next
            # change made by a user.
            self.threads_edited_by_bot.pop(thread.id, None)
            raise

    async def poll_github(self) -> None:
        await self.wait_until_ready()
        channel = self.get_channel(self.settings.discord_channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
            raise RuntimeError("DISCORD_CHANNEL_ID must point to a text or forum channel")

        while not self.is_closed():
            try:
                items = {
                    key: item
                    for item in await self.github.list_items()
                    if (key := github_item_key(item))
                }
                known = bool(self.known_github_items)
                if not known:
                    await self.reconcile_links(channel, items)
                for key, item in items.items():
                    if not known:
                        self.known_github_states[key] = self.github_state(item)
                    elif key not in self.known_github_items:
                        details = github_item_data(
                            item,
                            self.assignee_field_name,
                            self.settings.github_priority_field_name,
                        )
                        # Track the state of new tasks, so that their later
                        # changes are announced.
                        self.known_github_states[key] = task_state(details)
                        if not github_item_has_discord_marker(
                            item
                        ) and not github_item_source_url(item):
                            thread = await self.publish_to_discord(channel, details)
                            if thread is not None:
                                await self.link_github_item(key, item, thread)
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
                    elif key in self.known_github_states:
                        details = github_item_data(
                            item,
                            self.assignee_field_name,
                            self.settings.github_priority_field_name,
                        )
                        state = task_state(details)
                        if state != self.known_github_states[key]:
                            changes = state_changes(
                                self.known_github_states[key], state
                            )
                            written = self.github_writes_from_discord.pop(key, None)
                            self.known_github_states[key] = state
                            # A change made in Discord comes back here on
                            # the next poll: it is already announced, and the
                            # post already carries its values.
                            if written != (
                                details["title"],
                                details["status"],
                                details["priority"],
                            ):
                                await self.update_discord_from_github(item)
                                await self.notify(
                                    UPDATED,
                                    GITHUB_TO_DISCORD,
                                    details["title"],
                                    changes=changes,
                                    links={
                                        "GitHub": details["url"],
                                        "Discord": self.known_github_sources.get(
                                            key, ""
                                        ),
                                    },
                                )
                    if source_url := github_item_source_url(item):
                        self.known_github_sources[key] = source_url
                if known:
                    for key in self.known_github_items - items.keys():
                        source_url = self.known_github_sources.get(key)
                        title = self.known_github_states.get(key, ("",))[0]
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
            except Exception:
                logger.exception("GitHub polling failed")
            await asyncio.sleep(self.settings.github_poll_interval_seconds)

    def github_state(self, item: dict[str, object]) -> tuple[str, ...]:
        return task_state(
            github_item_data(
                item,
                self.assignee_field_name,
                self.settings.github_priority_field_name,
            )
        )

    async def close(self) -> None:
        if self.poll_task:
            self.poll_task.cancel()
        await self.github.close()
        await super().close()


async def run() -> None:
    load_dotenv()
    settings = Settings.from_environment()
    github = GitHubClient(
        settings.github_token,
        settings.github_project_id,
        settings.github_repository_id,
        settings.github_priority_field_name,
        settings.github_assignee_field_name,
        settings.github_org_name,
        settings.github_project_number,
    )
    bot = HorizonBot(settings, github)
    await bot.start(settings.discord_token)


if __name__ == "__main__":
    asyncio.run(run())
