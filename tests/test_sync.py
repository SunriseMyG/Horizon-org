import unittest
from types import SimpleNamespace

from bot_horizon.sync import discord_message_body, github_item_key, is_bot_message


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


if __name__ == "__main__":
    unittest.main()
