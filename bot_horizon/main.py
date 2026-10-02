import asyncio
import logging
from urllib.parse import urlparse

import discord
from dotenv import load_dotenv

from .config import Settings
from .github_client import GitHubClient
from .sync import (
    DISCORD_MARKER,
    discord_message_body,
    discord_task_data,
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
            )
            logger.info("Updated GitHub issue from Discord thread: %s", after.jump_url)
        except Exception:
            logger.exception("Could not update GitHub issue from Discord thread")

    def is_configured_channel(self, channel: discord.abc.GuildChannel) -> bool:
        return channel.id == self.settings.discord_channel_id or getattr(
            channel, "parent_id", None
        ) == self.settings.discord_channel_id

    async def publish_to_discord(
        self, channel: discord.abc.GuildChannel, item: dict[str, str]
    ) -> None:
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
        elif isinstance(channel, discord.TextChannel):
            await channel.send(content)
        else:
            raise RuntimeError("DISCORD_CHANNEL_ID must point to a text or forum channel")

    async def update_discord_from_github(self, item: dict[str, object]) -> None:
        source_url = github_item_source_url(item)
        if not source_url:
            return
        parts = urlparse(source_url).path.strip("/").split("/")
        if len(parts) < 4:
            return
        thread = self.get_channel(int(parts[2]))
        if not isinstance(thread, discord.Thread) or not isinstance(
            thread.parent, discord.ForumChannel
        ):
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
                items = await self.github.list_items()
                current_keys = {key for item in items if (key := github_item_key(item))}
                if not self.known_github_items:
                    self.known_github_items = current_keys
                    self.known_github_states = {
                        key: self.github_state(item)
                        for item in items
                        if (key := github_item_key(item))
                    }
                else:
                    for item in items:
                        key = github_item_key(item)
                        if (
                            key
                            and key not in self.known_github_items
                            and not github_item_has_discord_marker(item)
                        ):
                            await self.publish_to_discord(
                                channel,
                                github_item_data(
                                    item,
                                    self.assignee_field_name,
                                    self.settings.github_priority_field_name,
                                ),
                            )
                        elif key and key in self.known_github_states:
                            state = self.github_state(item)
                            if state != self.known_github_states[key]:
                                await self.update_discord_from_github(item)
                            self.known_github_states[key] = state
                    self.known_github_items = current_keys
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
