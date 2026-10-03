import unittest
from types import SimpleNamespace

from bot_horizon.sync import (
    discord_message_body,
    discord_task_data,
    github_item_data,
    discord_thread_id,
    github_item_key,
    parse_assignees,
    is_bot_message,
)


class SyncTests(unittest.TestCase):
    def test_github_item_key_prefers_content_url(self) -> None:
        item = {"id": "PVTI_item", "content": {"url": "https://github.com/org/repo/issues/1"}}
        self.assertEqual(github_item_key(item), "https://github.com/org/repo/issues/1")

    def test_github_item_key_falls_back_to_project_item_id(self) -> None:
        self.assertEqual(github_item_key({"id": "PVTI_item", "content": None}), "PVTI_item")

    def test_bot_messages_are_ignored(self) -> None:
        message = SimpleNamespace(author=SimpleNamespace(bot=True))
        self.assertTrue(is_bot_message(message))

    def test_discord_body_keeps_source_link(self) -> None:
        message = SimpleNamespace(
            author=SimpleNamespace(display_name="Alice"),
            content="Fix login",
            jump_url="https://discord.com/channels/1/2/3",
        )
        body = discord_message_body(message)
        self.assertIn("Status: Backlog", body)
        self.assertIn("Priority: Low", body)
        self.assertIn("Description:\nFix login", body)
        self.assertIn(message.jump_url, body)

    def test_thread_id_from_message_jump_url(self) -> None:
        url = "https://discord.com/channels/111/222/333"
        self.assertEqual(discord_thread_id(url), 222)

    def test_thread_id_from_thread_jump_url(self) -> None:
        self.assertEqual(discord_thread_id("https://discord.com/channels/111/222"), 222)

    def test_thread_id_rejects_unrelated_urls(self) -> None:
        self.assertIsNone(discord_thread_id(None))
        self.assertIsNone(discord_thread_id(""))
        self.assertIsNone(discord_thread_id("https://github.com/org/repo/issues/1"))
        self.assertIsNone(discord_thread_id("https://discord.com/channels/111/abc"))


    def test_assignees_accept_commas_and_spaces(self) -> None:
        for text in ("alice, bob", "alice bob", "alice;bob", "@alice, @bob"):
            self.assertEqual(parse_assignees(text), ["alice", "bob"], text)

    def test_assignees_drop_duplicates_and_unassigned(self) -> None:
        self.assertEqual(parse_assignees("alice, Alice, bob"), ["alice", "bob"])
        self.assertEqual(parse_assignees("Unassigned"), [])
        self.assertEqual(parse_assignees(""), [])

    def test_discord_task_reads_several_assignees(self) -> None:
        message = SimpleNamespace(
            author=SimpleNamespace(display_name="Alice"),
            content="Assigned to: alice, bob\nDescription:\nFix login",
            jump_url="https://discord.com/channels/1/2/3",
        )
        task = discord_task_data(message)
        self.assertEqual(task["assignees"], ["alice", "bob"])
        self.assertEqual(task["assignee"], "alice, bob")

    def test_github_item_keeps_every_assignee(self) -> None:
        item = {
            "content": {
                "title": "T",
                "assignees": {"nodes": [{"login": "alice"}, {"login": "bob"}]},
            },
            "fieldValues": {"nodes": []},
        }
        details = github_item_data(item)
        self.assertEqual(details["assignees"], ["alice", "bob"])
        self.assertEqual(details["assignee"], "alice, bob")

    def test_github_item_falls_back_to_the_owner_field(self) -> None:
        item = {
            "content": {"title": "T"},
            "fieldValues": {
                "nodes": [
                    {"text": "alice, bob", "field": {"name": "Owner"}},
                ]
            },
        }
        self.assertEqual(github_item_data(item)["assignees"], ["alice", "bob"])


if __name__ == "__main__":
    unittest.main()
