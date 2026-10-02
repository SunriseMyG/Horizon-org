import asyncio
import logging

import discord
from dotenv import load_dotenv

from .config import Settings
from .github_client import GitHubClient
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
)


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


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
        self.poll_task: asyncio.Task[None] | None = None

    async def on_ready(self) -> None:
        logger.info("Connected as %s", self.user)
        if self.poll_task is None:
            self.poll_task = asyncio.create_task(self.poll_github())

    async def on_message(self, message: discord.Message) -> None:
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
                assignee=task["assignee"],
            )
            logger.info("Created GitHub issue from Discord message: %s", issue_url)
        except Exception:
            logger.exception("Could not create a GitHub issue from Discord message")

    async def on_thread_update(
        self, before: discord.Thread, after: discord.Thread
    ) -> None:
        if not self.is_configured_channel(after):
            return
        try:
            starter_message = await after.fetch_message(after.id)
            if is_bot_message(starter_message):
                return
            task = discord_task_data(starter_message, after)
            await self.github.update_issue_from_discord(
                after.jump_url,
                task["title"],
                task["status"],
                task["priority"],
                previous_title=before.name,
            )
            logger.info("Updated GitHub issue from Discord thread: %s", after.jump_url)
        except Exception:
            logger.exception("Could not update GitHub issue from Discord thread")

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
        await thread.edit(name=details["title"][:100], applied_tags=tags)

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
                        if not github_item_has_discord_marker(
                            item
                        ) and not github_item_source_url(item):
                            thread = await self.publish_to_discord(
                                channel,
                                github_item_data(
                                    item,
                                    self.assignee_field_name,
                                    self.settings.github_priority_field_name,
                                ),
                            )
                            if thread is not None:
                                await self.link_github_item(key, item, thread)
                    elif key in self.known_github_states:
                        state = self.github_state(item)
                        if state != self.known_github_states[key]:
                            await self.update_discord_from_github(item)
                        self.known_github_states[key] = state
                    if source_url := github_item_source_url(item):
                        self.known_github_sources[key] = source_url
                if known:
                    for key in self.known_github_items - items.keys():
                        await self.delete_discord_thread(
                            self.known_github_sources.get(key)
                        )
                        self.forget_github_item(key)
                self.known_github_items = set(items)
            except Exception:
                logger.exception("GitHub polling failed")
            await asyncio.sleep(self.settings.github_poll_interval_seconds)

    def github_state(self, item: dict[str, object]) -> tuple[str, ...]:
        details = github_item_data(
            item,
            self.assignee_field_name,
            self.settings.github_priority_field_name,
        )
        return (
            details["title"],
            details["status"],
            details["priority"],
            details["assignee"],
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
