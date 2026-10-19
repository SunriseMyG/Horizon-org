import unittest
from types import SimpleNamespace

from bot_horizon.sync import (
    DISCORD_MESSAGE_LIMIT,
    GITHUB_MARKER,
    card_state,
    issue_body,
    issue_description,
    same_card,
    with_description,
    discord_comment_body,
    discord_message_body,
    discord_task_data,
    github_comment_message,
    github_item_comments,
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
        self.assertTrue(body.startswith(GITHUB_MARKER))
        self.assertIn("Fix login", body)
        self.assertIn(f"Source: {message.jump_url}", body)

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


class CommentFormatTests(unittest.TestCase):
    def test_draft_items_have_no_comments(self) -> None:
        self.assertEqual(github_item_comments({"content": {"title": "draft"}}), [])

    def test_a_deleted_author_reads_as_ghost(self) -> None:
        item = {
            "content": {
                "comments": {"nodes": [{"id": "C1", "body": "hi", "author": None}]}
            }
        }
        self.assertEqual(github_item_comments(item)[0]["author"], "ghost")

    def test_a_long_comment_fits_in_one_discord_message(self) -> None:
        comment = {
            "id": "C1",
            "url": "https://github.com/o/r/issues/1#c1",
            "body": "x" * 5000,
            "author": "bob",
        }
        text = github_comment_message(comment)
        self.assertLessEqual(len(text), DISCORD_MESSAGE_LIMIT)
        self.assertTrue(text.endswith(f"<{comment['url']}>"))

    def test_a_discord_reply_keeps_readable_mentions(self) -> None:
        message = SimpleNamespace(
            author=SimpleNamespace(display_name="Alice"),
            content="<@123> look",
            clean_content="@bob look",
            attachments=[],
            jump_url="https://discord.com/channels/1/2/3",
        )
        body = discord_comment_body(message)
        self.assertIn("@bob look", body)
        self.assertNotIn("<@123>", body)


class IssueBodyTests(unittest.TestCase):
    def test_the_description_comes_back_from_the_body_the_bot_writes(self) -> None:
        body = issue_body("Fix the login\n\nOn mobile too.", "https://discord.com/channels/1/2")
        self.assertEqual(issue_description(body), "Fix the login\n\nOn mobile too.")

    def test_an_older_body_keeps_only_its_description(self) -> None:
        body = (
            f"{GITHUB_MARKER}\nStatus: Backlog\nPriority: Low\nAssigned to: alice\n\n"
            "Description:\nFix login\n\nSource: https://discord.com/channels/1/2/3"
        )
        self.assertEqual(issue_description(body), "Fix login")

    def test_a_body_written_on_github_is_its_own_description(self) -> None:
        body = "Steps to reproduce\n\nSource: https://discord.com/channels/1/2"
        self.assertEqual(issue_description(body), "Steps to reproduce")

    def test_a_new_description_keeps_the_marker_and_the_link(self) -> None:
        body = issue_body("Old", "https://discord.com/channels/1/2")
        rewritten = with_description(body, "New")
        self.assertTrue(rewritten.startswith(GITHUB_MARKER))
        self.assertIn("Source: https://discord.com/channels/1/2", rewritten)
        self.assertEqual(issue_description(rewritten), "New")

    def test_a_github_body_gets_no_marker(self) -> None:
        rewritten = with_description("Old\n\nSource: https://discord.com/channels/1/2", "New")
        self.assertNotIn(GITHUB_MARKER, rewritten)
        self.assertEqual(issue_description(rewritten), "New")


class ProjectFieldTests(unittest.TestCase):
    def item(self, **content) -> dict:
        return {
            "content": {"title": "T", "url": "https://github.com/o/r/issues/1", **content},
            "fieldValues": {
                "nodes": [
                    {"name": "Ready", "field": {"name": "Status"}},
                    {"name": "M", "field": {"name": "Size"}},
                    {"date": "2026-10-20", "field": {"name": "Start date"}},
                    {"date": "2026-10-31", "field": {"name": "Target date"}},
                ]
            },
        }

    def test_the_size_and_the_dates_are_read(self) -> None:
        details = github_item_data(self.item())
        self.assertEqual(
            (details["size"], details["start_date"], details["target_date"]),
            ("M", "2026-10-20", "2026-10-31"),
        )

    def test_the_dates_of_issue_fields_are_read_from_the_issue(self) -> None:
        item = {
            "content": {
                "title": "T",
                "issueFieldValues": {
                    "nodes": [
                        {"value": "2026-11-15", "field": {"name": "Target date"}},
                        {"name": "Urgent", "field": {"name": "Task Priority"}},
                        {},
                    ]
                },
            },
            "fieldValues": {"nodes": []},
        }
        details = github_item_data(item)
        self.assertEqual(details["target_date"], "2026-11-15")
        self.assertEqual(details["priority"], "Urgent")

    def test_a_closed_issue_reads_as_closed(self) -> None:
        self.assertTrue(github_item_data(self.item(state="CLOSED"))["closed"])
        self.assertFalse(github_item_data(self.item(state="OPEN"))["closed"])

    def test_the_description_drops_the_source_line(self) -> None:
        details = github_item_data(
            self.item(body=issue_body("Do it", "https://discord.com/channels/1/2"))
        )
        self.assertEqual(details["description"], "Do it")

    def test_the_login_case_does_not_make_a_change(self) -> None:
        first = github_item_data(self.item(assignees={"nodes": [{"login": "SunriseMyG"}]}))
        second = {**first, "assignee": "sunrisemyg"}
        self.assertNotEqual(card_state(first), card_state(second))
        self.assertTrue(same_card(first, second))


if __name__ == "__main__":
    unittest.main()
