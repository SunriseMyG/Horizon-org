"""The bot end to end, on a fake GitHub and fake Discord objects.

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
from bot_horizon.form import TaskError
from bot_horizon.main import HorizonBot
from bot_horizon.sync import GITHUB_MARKER

FORUM_ID = 100
THREAD_ID = 200
NOTIFICATION_ID = 300
BOT_ID = 999
THREAD_URL = f"https://discord.com/channels/1/{THREAD_ID}"
KEY = "https://github.com/play-horizon/horizon/issues/20"
TAGS = ("Backlog", "Ready", "In progress", "In review", "Done", "Low", "High")


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


def forum() -> discord.ForumChannel:
    """A forum channel carrying the status and priority tags."""
    channel = object.__new__(discord.ForumChannel)
    channel.id = FORUM_ID
    channel._available_tags = {
        index: SimpleNamespace(id=index, name=name) for index, name in enumerate(TAGS)
    }
    return channel


class FakeThread(SimpleNamespace):
    """A forum post whose edits change it, the way Discord applies them."""

    def __init__(self, *tags: str, name: str = "testdev", owner_id: int = 1) -> None:
        super().__init__(
            id=THREAD_ID,
            name=name,
            parent_id=FORUM_ID,
            parent=forum(),
            owner_id=owner_id,
            archived=False,
            applied_tags=[SimpleNamespace(name=tag) for tag in tags],
            jump_url=THREAD_URL,
            starter_message=None,
            send=AsyncMock(return_value=SimpleNamespace(id=777, jump_url=f"{THREAD_URL}/777")),
            fetch_message=AsyncMock(),
            history=self.history,
            get_partial_message=lambda message_id: self.messages[message_id],
            messages={},
        )
        self.edit = AsyncMock(side_effect=self.apply)

    async def apply(self, **changes) -> "FakeThread":
        for key, value in changes.items():
            setattr(self, key, value)
        return self

    async def history(self, limit=None):
        for message in self.messages.values():
            yield message


def github_comment(comment_id: str, body: str, login: str = "bob") -> dict:
    return {
        "id": comment_id,
        "url": f"{KEY}#{comment_id}",
        "body": body,
        "author": {"login": login},
    }


class FakeGitHub:
    """One task, stored the way the GitHub listing returns it."""

    def __init__(self) -> None:
        self.task = {
            "title": "testdev",
            "status": "Backlog",
            "priority": "Low",
            "assignees": ["alice"],
            "description": "A task",
            "size": "",
            "start_date": "",
            "target_date": "",
            "closed": False,
        }
        self.comments: list[dict[str, object]] = []
        self.writes: list[dict] = []
        self.item_keys: list[str | None] = []
        self.created: list[tuple[str, str, dict]] = []
        self.commented: list[tuple[str, str, str | None]] = []
        self.edited_comments: list[tuple[str, str]] = []
        self.deleted_comments: list[str] = []
        self.statuses: list[str] = []

    def item(self) -> dict[str, object]:
        task = self.task
        fields = [
            {"name": task["status"], "field": {"name": "Status"}},
            {"name": task["priority"], "field": {"name": "Task Priority"}},
        ]
        if task["size"]:
            fields.append({"name": task["size"], "field": {"name": "Size"}})
        for key, name in (("start_date", "Start date"), ("target_date", "Target date")):
            if task[key]:
                fields.append({"date": task[key], "field": {"name": name}})
        return {
            "id": "PVTI_1",
            "content": {
                "id": "I_1",
                "url": KEY,
                "title": task["title"],
                "state": "CLOSED" if task["closed"] else "OPEN",
                "body": f"{task['description']}\n\nSource: {THREAD_URL}",
                "assignees": {"nodes": [{"login": login} for login in task["assignees"]]},
                "comments": {"totalCount": len(self.comments), "nodes": self.comments},
            },
            "fieldValues": {"nodes": fields},
        }

    def write(self, **changes) -> None:
        changes = {key: value for key, value in changes.items() if value is not None}
        self.writes.append(changes)
        self.task.update(changes)

    async def create_issue(self, title, body, status="Backlog", priority="Low", assignees="", **fields):
        self.created.append((title, body, {"status": status, "priority": priority, "assignees": assignees, **fields}))
        return KEY

    async def comment_from_discord(self, source_url, body, item_key=None):
        self.commented.append((source_url, body, item_key))
        return f"{KEY}#issuecomment-1"

    async def edit_comment_from_discord(self, source_url, message_url, body, item_key=None):
        self.edited_comments.append((message_url, body))
        return f"{KEY}#issuecomment-1"

    async def delete_comment_from_discord(self, source_url, message_url, item_key=None):
        self.deleted_comments.append(message_url)
        return f"{KEY}#issuecomment-1"

    async def update_issue_from_discord(
        self, source_url, title, status, priority, previous_title=None, item_key=None, closed=None
    ):
        self.item_keys.append(item_key)
        self.write(title=title, status=status, priority=priority, closed=closed)
        return KEY

    async def update_linked_task(self, source_url, item_key=None, **changes):
        self.item_keys.append(item_key)
        self.write(**changes)
        return KEY

    async def update_task(self, item, **changes):
        self.write(**changes)

    async def update_project_status(self, item_id, status):
        self.statuses.append(status)
        self.task["status"] = status

    async def find_linked_item(self, source_url, item_key=None, title=None):
        return self.item()

    async def project_field(self, name):
        if name == "Size":
            return {"id": "F_size", "name": "Size", "options": [{"name": "S"}, {"name": "M"}]}
        return {"id": f"F_{name}", "name": name, "dataType": "DATE"}

    async def assignable_users(self):
        return ["alice", "bob"]

    async def list_items(self):
        return [self.item()]

    async def close(self):
        pass


class BotTestCase(unittest.IsolatedAsyncioTestCase):
    """A bot that already knows the task, and records its notifications."""

    def setUp(self) -> None:
        self.github = FakeGitHub()
        self.bot = HorizonBot(settings(), self.github)
        self.post = FakeThread("Backlog", "Low")
        self.notified: list[tuple] = []
        notified = self.notified

        async def fake_notify(bot, action, direction, title, **kwargs):
            notified.append((action, direction, kwargs.get("changes")))
            self.last_notification = kwargs

        for patcher in (
            patch.object(HorizonBot, "notify", fake_notify),
            patch.object(HorizonBot, "user", SimpleNamespace(id=BOT_ID)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def poll_once(self) -> None:
        """One poll iteration, where the forum post resolves to `self.post`."""
        with patch.object(
            HorizonBot, "resolve_thread", AsyncMock(return_value=self.post)
        ):
            await self.bot.poll_once(forum())


class KnownTaskTestCase(BotTestCase):
    async def asyncSetUp(self) -> None:
        # The first poll is the baseline: the bot learns the task silently.
        await self.poll_once()
        self.assertEqual(self.notified, [])


class StartTests(BotTestCase):
    """The first poll, after a start: the tasks already there."""

    def setUp(self) -> None:
        super().setUp()
        self.forum = forum()
        self.published = AsyncMock(return_value=self.post)
        self.github.set_discord_source = AsyncMock()
        # A task created on GitHub that no post was ever opened for.
        self.github.item = lambda: {
            "id": "PVTI_1",
            "content": {"id": "I_1", "url": KEY, "title": "Never published", "body": "", "state": "OPEN"},
            "fieldValues": {"nodes": [{"name": "Ready", "field": {"name": "Status"}}]},
        }

    async def start(self, posts=(), complete=True) -> None:
        threads = [
            SimpleNamespace(id=900 + index, name=name, jump_url=f"https://discord.com/channels/1/{900 + index}")
            for index, name in enumerate(posts)
        ]
        with patch.object(
            HorizonBot, "forum_threads", AsyncMock(return_value=(threads, complete))
        ), patch.object(HorizonBot, "publish_to_discord", self.published), patch.object(
            HorizonBot, "resolve_thread", AsyncMock(return_value=self.post)
        ):
            await self.bot.poll_once(self.forum)

    async def test_a_task_without_any_post_is_published_without_a_card(self) -> None:
        await self.start(posts=["Another task"])
        self.published.assert_awaited_once()
        self.github.set_discord_source.assert_awaited_once()
        self.assertEqual(self.bot.known_github_sources[KEY], THREAD_URL)
        self.assertEqual(self.notified, [])

    async def test_a_task_whose_post_exists_is_linked_not_published_again(self) -> None:
        await self.start(posts=["Never published"])
        self.published.assert_not_awaited()
        self.github.set_discord_source.assert_awaited_once()

    async def test_nothing_is_published_without_the_archived_posts(self) -> None:
        await self.start(posts=[], complete=False)
        self.published.assert_not_awaited()

    async def test_a_linked_task_is_left_alone(self) -> None:
        self.github.item = FakeGitHub().item
        await self.start()
        self.published.assert_not_awaited()
        self.github.set_discord_source.assert_not_awaited()

    async def test_a_task_that_cannot_be_published_does_not_stop_the_start(self) -> None:
        self.published.side_effect = RuntimeError("No matching Discord forum tags")
        await self.start()
        self.assertEqual(self.bot.known_github_items, {KEY})


class EchoTests(KnownTaskTestCase):
    async def test_one_tag_change_sends_one_notification(self) -> None:
        await self.bot.on_thread_update(
            FakeThread("Backlog", "Low"), FakeThread("In progress", "Low")
        )
        await self.poll_once()
        self.assertEqual(len(self.github.writes), 1)
        self.assertEqual(
            self.notified,
            [("updated", "Discord → GitHub", [("status", "Backlog", "In progress")])],
        )
        self.post.edit.assert_not_awaited()

    async def test_an_update_without_change_is_ignored(self) -> None:
        await self.bot.on_thread_update(
            FakeThread("In progress", "Low"), FakeThread("In progress", "Low")
        )
        self.assertEqual(self.github.writes, [])
        self.assertEqual(self.notified, [])

    async def test_the_bot_own_edit_of_a_post_is_ignored(self) -> None:
        self.bot.threads_edited_by_bot[THREAD_ID] = (
            "testdev",
            frozenset({"In progress", "Low"}),
        )
        await self.bot.on_thread_update(
            FakeThread("Backlog", "Low"), FakeThread("In progress", "Low")
        )
        self.assertEqual(self.github.writes, [])
        self.assertEqual(self.notified, [])

    async def test_a_change_made_on_github_is_announced_once(self) -> None:
        self.github.task["status"] = "In review"
        await self.poll_once()
        self.assertEqual(
            self.notified,
            [("updated", "GitHub → Discord", [("status", "Backlog", "In review")])],
        )
        self.notified.clear()
        await self.poll_once()
        self.assertEqual(self.notified, [])

    async def test_two_quick_discord_changes_are_not_undone_by_a_late_listing(self) -> None:
        await self.bot.on_thread_update(
            FakeThread("Backlog", "Low"), FakeThread("Ready", "Low")
        )
        listing_after_the_first = self.github.item()
        await self.bot.on_thread_update(
            FakeThread("Ready", "Low"), FakeThread("In progress", "Low")
        )
        with patch.object(
            self.github, "list_items", AsyncMock(return_value=[listing_after_the_first])
        ):
            await self.poll_once()
        await self.poll_once()
        self.post.edit.assert_not_awaited()
        self.assertEqual([n[1] for n in self.notified], ["Discord → GitHub"] * 2)

    async def test_renaming_a_post_renames_the_issue(self) -> None:
        await self.bot.on_thread_update(
            FakeThread("Backlog", "Low"), FakeThread("Backlog", "Low", name="Fix login")
        )
        self.assertEqual(self.github.task["title"], "Fix login")
        # The issue is found from the tracked key, without the Project listing.
        self.assertEqual(self.github.item_keys, [KEY])
        self.assertEqual(
            self.notified,
            [("updated", "Discord → GitHub", [("title", "testdev", "Fix login")])],
        )

    async def test_a_post_the_bot_opened_can_be_edited_from_discord(self) -> None:
        await self.bot.on_thread_update(
            FakeThread("Backlog", "Low", owner_id=BOT_ID),
            FakeThread("Backlog", "High", owner_id=BOT_ID),
        )
        self.assertEqual(self.github.task["priority"], "High")

    async def test_renaming_the_issue_renames_the_post(self) -> None:
        self.github.task["title"] = "Fix login"
        await self.poll_once()
        self.post.edit.assert_awaited_once()
        self.assertEqual(self.post.edit.await_args.kwargs["name"], "Fix login")
        self.assertEqual(
            self.notified,
            [("updated", "GitHub → Discord", [("title", "testdev", "Fix login")])],
        )

    async def test_new_dates_and_size_are_announced(self) -> None:
        self.github.task.update(size="M", target_date="2026-10-31")
        await self.poll_once()
        [(_, _, changes)] = self.notified
        self.assertEqual(
            changes, [("size", "", "M"), ("target_date", "", "2026-10-31")]
        )


class CloseTests(KnownTaskTestCase):
    async def test_the_done_tag_closes_the_issue_and_archives_the_post(self) -> None:
        post = FakeThread("Done", "Low")
        await self.bot.on_thread_update(FakeThread("In review", "Low"), post)
        self.assertTrue(self.github.task["closed"])
        self.assertTrue(post.archived)
        await self.poll_once()
        self.assertEqual(len(self.notified), 1)

    async def test_leaving_done_reopens_the_issue(self) -> None:
        self.github.task.update(status="Done", closed=True)
        await self.poll_once()
        self.notified.clear()
        await self.bot.on_thread_update(
            FakeThread("Done", "Low"), FakeThread("In progress", "Low")
        )
        self.assertFalse(self.github.task["closed"])

    async def test_closing_the_issue_moves_it_to_done_and_archives_the_post(self) -> None:
        self.github.task["closed"] = True
        await self.poll_once()
        self.assertEqual(self.github.statuses, ["Done"])
        self.assertTrue(self.post.archived)
        self.assertIn("Done", {tag.name for tag in self.post.applied_tags})
        self.assertEqual(
            self.notified,
            [("updated", "GitHub → Discord", [("status", "Backlog", "Done")])],
        )

    async def test_reopening_the_issue_brings_the_post_back(self) -> None:
        self.github.task.update(status="Done", closed=True)
        await self.poll_once()
        self.github.task["closed"] = False
        await self.poll_once()
        self.assertEqual(self.github.statuses[-1], "In progress")
        self.assertFalse(self.post.archived)


class PostCardTests(KnownTaskTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.post.owner_id = BOT_ID
        self.starter = SimpleNamespace(content="Assigned to: alice", edit=AsyncMock())
        self.post.starter_message = self.starter

    async def test_a_post_the_bot_opened_shows_the_task_as_it_changes(self) -> None:
        self.github.task.update(description="A better description", target_date="2026-10-31")
        await self.poll_once()
        card = self.starter.edit.await_args.kwargs["content"]
        self.assertIn("A better description", card)
        self.assertIn("<t:", card)
        self.assertIn(KEY, card)

    async def test_an_archived_post_is_opened_for_the_edit_then_archived_again(self) -> None:
        self.post.archived = True
        self.github.task["description"] = "Changed"
        await self.poll_once()
        self.starter.edit.assert_awaited_once()
        self.assertTrue(self.post.archived)
        self.assertEqual(
            [call.kwargs for call in self.post.edit.await_args_list],
            [{"archived": False}, {"archived": True}],
        )


class OpeningMessageTests(KnownTaskTestCase):
    def payload(self, content: str, before: str | None = None) -> SimpleNamespace:
        message = SimpleNamespace(
            id=THREAD_ID,
            content=content,
            attachments=[],
            author=SimpleNamespace(bot=False),
            channel=self.post,
        )
        cached = None if before is None else SimpleNamespace(content=before, attachments=[])
        return SimpleNamespace(message=message, cached_message=cached)

    async def test_editing_the_assignees_of_a_post_assigns_the_issue(self) -> None:
        await self.bot.on_raw_message_edit(
            self.payload("Assigned to: alice, bob\nDescription:\nA task")
        )
        self.assertEqual(self.github.task["assignees"], ["alice", "bob"])
        self.assertEqual(
            self.notified,
            [("updated", "Discord → GitHub", [("assignee", "alice", "alice, bob")])],
        )
        await self.poll_once()
        self.assertEqual(len(self.notified), 1)

    async def test_an_edit_that_changes_nothing_writes_nothing(self) -> None:
        await self.bot.on_raw_message_edit(
            self.payload("Assigned to: alice\nDescription:\nA task")
        )
        self.assertEqual(self.github.writes, [])

    async def test_a_link_unfurl_is_not_an_edit(self) -> None:
        await self.bot.on_raw_message_edit(self.payload("same", before="same"))
        self.assertEqual(self.github.writes, [])


class CommentTests(BotTestCase):
    def message(
        self, message_id: int, content: str, *, bot: bool = False, attachments=()
    ) -> SimpleNamespace:
        return SimpleNamespace(
            id=message_id,
            content=content,
            clean_content=content,
            attachments=list(attachments),
            author=SimpleNamespace(bot=bot, display_name="Alice"),
            channel=self.post,
            jump_url=f"{THREAD_URL}/{message_id}",
        )

    async def test_a_reply_in_a_post_becomes_a_comment_not_a_task(self) -> None:
        await self.bot.on_message(self.message(201, "On it"))
        self.assertEqual(self.github.created, [])
        [(source_url, body, _)] = self.github.commented
        self.assertEqual(source_url, THREAD_URL)
        self.assertIn(GITHUB_MARKER, body)
        self.assertIn("**Alice** on [Discord]", body)
        self.assertIn("On it", body)
        self.assertEqual(self.notified, [("commented", "Discord → GitHub", None)])
        self.assertEqual(self.last_notification["message"], ("Alice", "On it"))

    async def test_a_reply_right_after_the_creation_finds_its_issue(self) -> None:
        await self.bot.on_message(
            self.message(THREAD_ID, "Assigned to: alice\nDescription:\nA task")
        )
        await self.bot.on_message(self.message(201, "On it"))
        # The issue url is known without waiting for the Project listing.
        [(_, _, item_key)] = self.github.commented
        self.assertEqual(item_key, KEY)

    async def test_the_opening_message_still_creates_the_task(self) -> None:
        await self.bot.on_message(
            self.message(THREAD_ID, "Assigned to: alice\nDescription:\nA task")
        )
        self.assertEqual(len(self.github.created), 1)
        self.assertEqual(self.github.commented, [])

    async def test_an_attachment_alone_is_copied(self) -> None:
        attachment = SimpleNamespace(filename="shot.png", url="https://cdn/shot.png")
        await self.bot.on_message(self.message(201, "", attachments=[attachment]))
        [(_, body, _)] = self.github.commented
        self.assertIn("[shot.png](https://cdn/shot.png)", body)

    async def test_a_bot_reply_is_not_copied(self) -> None:
        await self.bot.on_message(self.message(201, "bob on GitHub", bot=True))
        self.assertEqual(self.github.commented, [])

    async def test_editing_a_reply_edits_its_comment(self) -> None:
        message = self.message(201, "On it, tomorrow")
        await self.bot.on_raw_message_edit(
            SimpleNamespace(message=message, cached_message=None)
        )
        [(message_url, body)] = self.github.edited_comments
        self.assertEqual(message_url, f"{THREAD_URL}/201")
        self.assertIn("On it, tomorrow", body)

    async def test_deleting_a_reply_deletes_its_comment(self) -> None:
        with patch.object(HorizonBot, "get_channel", return_value=self.post):
            await self.bot.on_raw_message_delete(
                SimpleNamespace(
                    message_id=201, channel_id=THREAD_ID, guild_id=1, cached_message=None
                )
            )
        self.assertEqual(self.github.deleted_comments, [f"{THREAD_URL}/201"])

    async def test_the_first_poll_does_not_post_the_existing_comments(self) -> None:
        self.github.comments = [github_comment("C1", "old news")]
        await self.poll_once()
        self.post.send.assert_not_awaited()

    async def test_a_new_github_comment_is_posted_once(self) -> None:
        await self.poll_once()
        self.github.comments = [github_comment("C1", "Looks good @everyone")]
        await self.poll_once()
        await self.poll_once()
        self.post.send.assert_awaited_once()
        text = self.post.send.await_args.args[0]
        self.assertIn("**bob** on GitHub:", text)
        self.assertIn("Looks good", text)
        self.assertIn(f"<{KEY}#C1>", text)
        # A GitHub comment cannot ping the Discord server.
        mentions = self.post.send.await_args.kwargs["allowed_mentions"]
        self.assertFalse(mentions.everyone)
        self.assertEqual(self.notified, [("commented", "GitHub → Discord", None)])
        self.assertEqual(
            self.last_notification["links"],
            {"GitHub": f"{KEY}#C1", "Discord": f"{THREAD_URL}/777"},
        )

    async def test_an_edited_github_comment_rewrites_its_copy(self) -> None:
        await self.poll_once()
        self.github.comments = [github_comment("C1", "First")]
        await self.poll_once()
        copy = SimpleNamespace(id=777, edit=AsyncMock(), delete=AsyncMock())
        self.post.messages[777] = copy
        self.github.comments = [github_comment("C1", "Second thoughts")]
        await self.poll_once()
        self.assertIn("Second thoughts", copy.edit.await_args.kwargs["content"])

    async def test_a_deleted_github_comment_deletes_its_copy(self) -> None:
        await self.poll_once()
        self.github.comments = [github_comment("C1", "Oops")]
        await self.poll_once()
        copy = SimpleNamespace(id=777, edit=AsyncMock(), delete=AsyncMock())
        self.post.messages[777] = copy
        self.github.comments = []
        await self.poll_once()
        copy.delete.assert_awaited_once()
        # Its delete event is the bot's own: nothing to delete on GitHub.
        await self.bot.on_raw_message_delete(
            SimpleNamespace(message_id=777, channel_id=THREAD_ID, guild_id=1, cached_message=None)
        )
        self.assertEqual(self.github.deleted_comments, [])

    async def test_a_comment_copied_from_discord_does_not_come_back(self) -> None:
        await self.poll_once()
        self.github.comments = [
            github_comment("C1", f"{GITHUB_MARKER}\n**Alice** on Discord", "me")
        ]
        await self.poll_once()
        self.post.send.assert_not_awaited()
        self.assertEqual(self.notified, [])


class FormTests(KnownTaskTestCase):
    def draft(self, **changes) -> dict:
        return {
            "title": "Ship the form",
            "description": "From Discord",
            "status": "Ready",
            "priority": "High",
            "size": "M",
            "assignees": ["bob"],
            "start_date": "2026-10-20",
            "target_date": "2026-10-31",
            **changes,
        }

    async def create(self, **changes):
        starter = SimpleNamespace(edit=AsyncMock())
        created = (self.post, starter)
        channel = forum()
        with patch.object(HorizonBot, "get_channel", return_value=channel), patch.object(
            discord.ForumChannel, "create_thread", AsyncMock(return_value=created)
        ) as create_thread:
            thread = await self.bot.create_task_from_form(self.draft(**changes))
        return thread, starter, create_thread

    async def test_the_form_opens_a_post_and_its_issue(self) -> None:
        thread, starter, create_thread = await self.create()
        self.assertIs(thread, self.post)
        kwargs = create_thread.await_args.kwargs
        self.assertEqual(kwargs["name"], "Ship the form")
        self.assertEqual({tag.name for tag in kwargs["applied_tags"]}, {"Ready", "High"})
        [(title, body, fields)] = self.github.created
        self.assertIn(f"Source: {THREAD_URL}", body)
        self.assertEqual(
            (fields["size"], fields["start_date"], fields["target_date"], fields["assignees"]),
            ("M", "2026-10-20", "2026-10-31", ["bob"]),
        )
        # Once the issue exists, the opening message links to it.
        self.assertIn(KEY, starter.edit.await_args.kwargs["content"])
        self.assertEqual(self.notified, [("created", "Discord → GitHub", None)])

    async def test_a_post_whose_issue_fails_is_taken_back(self) -> None:
        self.github.create_issue = AsyncMock(side_effect=RuntimeError("down"))
        self.post.delete = AsyncMock()
        with self.assertRaises(RuntimeError):
            await self.create()
        self.post.delete.assert_awaited_once()
        self.assertIn(THREAD_ID, self.bot.threads_deleted_by_bot)

    async def test_saving_writes_only_what_changed(self) -> None:
        key, item, loaded = await self.bot.load_task_for_form(self.post)
        draft = {**loaded, "assignees": list(loaded["assignees"])}
        draft.update(status="In progress", target_date="2026-11-15")
        changed = await self.bot.save_task_from_form(self.post, key, item, loaded, draft)
        self.assertTrue(changed)
        self.assertEqual(
            self.github.writes, [{"status": "In progress", "target_date": "2026-11-15"}]
        )
        self.assertIn("In progress", {tag.name for tag in self.post.applied_tags})
        [(_, direction, changes)] = self.notified
        self.assertEqual(direction, "Discord → GitHub")
        self.assertEqual(
            changes,
            [("status", "Backlog", "In progress"), ("target_date", "", "2026-11-15")],
        )
        # The poll sees the bot's own write come back: no second card.
        await self.poll_once()
        self.assertEqual(len(self.notified), 1)

    async def test_saving_an_unchanged_task_writes_nothing(self) -> None:
        key, item, loaded = await self.bot.load_task_for_form(self.post)
        draft = {**loaded, "assignees": list(loaded["assignees"])}
        self.assertFalse(
            await self.bot.save_task_from_form(self.post, key, item, loaded, draft)
        )
        self.assertEqual(self.github.writes, [])

    async def test_done_from_the_form_closes_the_issue(self) -> None:
        key, item, loaded = await self.bot.load_task_for_form(self.post)
        draft = {**loaded, "assignees": list(loaded["assignees"]), "status": "Done"}
        await self.bot.save_task_from_form(self.post, key, item, loaded, draft)
        self.assertTrue(self.github.task["closed"])
        self.assertTrue(self.post.archived)

    async def test_a_post_outside_the_forum_is_refused(self) -> None:
        elsewhere = FakeThread()
        elsewhere.parent_id = 12345
        with self.assertRaises(TaskError) as raised:
            await self.bot.load_task_for_form(elsewhere)
        self.assertEqual(raised.exception.code, "not_a_task")

    async def test_the_form_offers_the_project_fields(self) -> None:
        options = await self.bot.form_options()
        self.assertEqual(options["sizes"], ["S", "M"])
        self.assertEqual(options["assignees"], ["alice", "bob"])
        self.assertTrue(options["start_date"] and options["target_date"])


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
