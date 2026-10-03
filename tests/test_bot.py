"""Echoes and the notification channel command, on fakes.

These tests need discord.py, so the project `.venv`:
`.venv\\Scripts\\python -m unittest tests.test_bot`.
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

try:
    import discord
except ImportError:  # pragma: no cover
    raise unittest.SkipTest("discord.py is not installed")

from bot_horizon.config import Settings
from bot_horizon.main import HorizonBot

FORUM_ID = 100
THREAD_ID = 200
NOTIFICATION_ID = 300
THREAD_URL = f"https://discord.com/channels/1/{THREAD_ID}"
KEY = "https://github.com/play-horizon/horizon/issues/20"


def settings(**overrides) -> Settings:
    return Settings(
        discord_token="t",
        discord_channel_id=FORUM_ID,
        github_token="g",
        github_project_id="p",
        github_priority_field_name="Task Priority",
        github_assignee_field_name="Owner",
        github_poll_interval_seconds=0,
        **overrides,
    )


def thread(*tags: str, name: str = "testdev") -> SimpleNamespace:
    return SimpleNamespace(
        id=THREAD_ID,
        name=name,
        parent_id=FORUM_ID,
        applied_tags=[SimpleNamespace(name=tag) for tag in tags],
        jump_url=THREAD_URL,
        fetch_message=AsyncMock(
            return_value=SimpleNamespace(
                author=SimpleNamespace(bot=False),
                content="Assigned to: alice\nDescription:\nA task",
                jump_url=THREAD_URL,
            )
        ),
    )


def github_item(status: str) -> dict[str, object]:
    """The task as GitHub returns it after the bot wrote to it."""
    return {
        "id": "PVTI_1",
        "content": {
            "id": "I_1",
            "url": KEY,
            "title": "testdev",
            "body": f"Source: {THREAD_URL}",
            "assignees": {"nodes": [{"login": "alice"}]},
        },
        "fieldValues": {
            "nodes": [
                {"name": status, "field": {"name": "Status"}},
                {"name": "Low", "field": {"name": "Task Priority"}},
            ]
        },
    }


class FakeGitHub:
    def __init__(self) -> None:
        self.status = "Backlog"
        self.writes = 0

    async def update_issue_from_discord(
        self, source_url, title, status, priority, previous_title=None
    ):
        self.writes += 1
        self.status = status
        return KEY

    async def list_items(self):
        return [github_item(self.status)]

    async def close(self):
        pass


class EchoTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.github = FakeGitHub()
        self.notified: list[tuple[str, str, list | None]] = []
        self.bot = HorizonBot(settings(), self.github)
        self.bot.known_github_items = {KEY}
        self.bot.known_github_states = {KEY: ("testdev", "Backlog", "Low", "alice")}
        self.bot.known_github_sources = {KEY: THREAD_URL}

        notified = self.notified

        async def fake_notify(bot, action, direction, title, **kwargs):
            notified.append((action, direction, kwargs.get("changes")))

        patcher = patch.object(HorizonBot, "notify", fake_notify)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def poll_once(self) -> None:
        """One poll iteration, on a channel forced to the right type."""
        forum = object.__new__(discord.ForumChannel)
        with patch.object(HorizonBot, "wait_until_ready", AsyncMock()), patch.object(
            HorizonBot, "get_channel", return_value=forum
        ), patch.object(HorizonBot, "is_closed", side_effect=[False, True]):
            await self.bot.poll_github()

    async def test_one_tag_change_sends_one_notification(self) -> None:
        await self.bot.on_thread_update(
            thread("Backlog", "Low"), thread("In progress", "Low")
        )
        await self.poll_once()
        self.assertEqual(self.github.writes, 1)
        self.assertEqual(
            self.notified,
            [("updated", "Discord → GitHub", [("status", "Backlog", "In progress")])],
        )

    async def test_an_update_without_change_is_ignored(self) -> None:
        await self.bot.on_thread_update(
            thread("In progress", "Low"), thread("In progress", "Low")
        )
        self.assertEqual(self.github.writes, 0)
        self.assertEqual(self.notified, [])

    async def test_the_bot_own_edit_of_a_post_is_ignored(self) -> None:
        self.bot.threads_edited_by_bot[THREAD_ID] = (
            "testdev",
            frozenset({"In progress", "Low"}),
        )
        await self.bot.on_thread_update(
            thread("Backlog", "Low"), thread("In progress", "Low")
        )
        self.assertEqual(self.github.writes, 0)
        self.assertEqual(self.notified, [])

    async def test_a_change_made_on_github_is_announced_once(self) -> None:
        self.github.status = "Done"
        await self.poll_once()
        self.assertEqual(
            self.notified,
            [("updated", "GitHub → Discord", [("status", "Backlog", "Done")])],
        )
        self.notified.clear()
        await self.poll_once()
        self.assertEqual(self.notified, [])


class LanguageCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.file = Path(self.directory.name) / "language"
        patcher = patch("bot_horizon.main.LANGUAGE_FILE", self.file)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.bot = HorizonBot(
            settings(discord_notification_channel_id=NOTIFICATION_ID), FakeGitHub()
        )

    def message(self, content: str, *, manage_guild: bool = True) -> SimpleNamespace:
        return SimpleNamespace(
            content=content,
            author=SimpleNamespace(
                bot=False, guild_permissions=SimpleNamespace(manage_guild=manage_guild)
            ),
            channel=SimpleNamespace(id=NOTIFICATION_ID, send=AsyncMock()),
        )

    async def test_the_command_switches_the_language_and_keeps_it(self) -> None:
        message = self.message("!lang fr")
        await self.bot.on_message(message)
        self.assertEqual(self.bot.language, "fr")
        self.assertEqual(self.file.read_text(encoding="utf-8"), "fr")
        # The confirmation carries a sample card in the new language.
        embed = message.channel.send.await_args.kwargs["embed"]
        self.assertIn("Tâche créée", embed.author.name)

    async def test_the_language_survives_a_restart(self) -> None:
        self.file.write_text("ru", encoding="utf-8")
        bot = HorizonBot(settings(discord_notification_channel_id=NOTIFICATION_ID), FakeGitHub())
        self.assertEqual(bot.language, "ru")

    async def test_an_unreadable_file_falls_back_to_the_environment(self) -> None:
        bot = HorizonBot(
            settings(
                discord_notification_channel_id=NOTIFICATION_ID,
                notification_language="es",
            ),
            FakeGitHub(),
        )
        self.assertEqual(bot.language, "es")

    async def test_an_unknown_code_is_refused(self) -> None:
        message = self.message("!lang klingon")
        await self.bot.on_message(message)
        self.assertEqual(self.bot.language, "en")
        self.assertIn("Unknown language", message.channel.send.await_args.args[0])

    async def test_the_command_needs_the_manage_server_permission(self) -> None:
        message = self.message("!lang fr", manage_guild=False)
        await self.bot.on_message(message)
        self.assertEqual(self.bot.language, "en")
        self.assertIn("manage the server", message.channel.send.await_args.args[0])
        self.assertFalse(self.file.exists())

    async def test_the_bare_command_lists_the_languages(self) -> None:
        message = self.message("!lang")
        await self.bot.on_message(message)
        listing = message.channel.send.await_args.args[0]
        for code in ("en", "zh", "fr", "ar", "ru"):
            self.assertIn(f"`{code}`", listing)

    async def test_an_ordinary_message_is_left_alone(self) -> None:
        message = self.message("hello everyone")
        await self.bot.on_message(message)
        message.channel.send.assert_not_awaited()

    async def test_a_notification_channel_message_never_becomes_a_task(self) -> None:
        github = FakeGitHub()
        github.create_issue = AsyncMock()
        bot = HorizonBot(settings(discord_notification_channel_id=NOTIFICATION_ID), github)
        await bot.on_message(self.message("Assigned to: alice"))
        github.create_issue.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
