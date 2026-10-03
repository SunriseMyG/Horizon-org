import unittest
from types import SimpleNamespace

from bot_horizon.notifications import (
    ACTION_COLORS,
    CREATED,
    DELETED,
    DISCORD_TO_GITHUB,
    GITHUB_TO_DISCORD,
    LANGUAGES,
    UPDATED,
    VALUE_LIMIT,
    build_notification,
    language_listing,
    language_words,
    notification_text,
    state_changes,
    task_details,
)
from bot_horizon.sync import discord_task_data, github_item_data, task_state


def thread(name: str, *tags: str) -> SimpleNamespace:
    return SimpleNamespace(
        name=name, applied_tags=[SimpleNamespace(name=tag) for tag in tags]
    )


def field_of(notification, label: str) -> tuple[str, str, bool]:
    return next(f for f in notification.fields if label in f[0])


class NotificationTests(unittest.TestCase):
    def creation(self, language: str = "en") -> object:
        task = {
            "title": "Fix login",
            "status": "Ready",
            "priority": "High",
            "assignee": "alice, bob",
        }
        return build_notification(
            CREATED,
            DISCORD_TO_GITHUB,
            task["title"],
            details=task_details(task),
            links={
                "GitHub": "https://github.com/org/repo/issues/1",
                "Discord": "https://discord.com/channels/1/2",
            },
            language=language,
        )

    def test_creation_card_carries_the_action_and_the_task_title(self) -> None:
        card = self.creation()
        self.assertIn("Task created", card.author)
        self.assertIn(DISCORD_TO_GITHUB, card.author)
        self.assertIn("Fix login", card.title)
        self.assertEqual(card.color, ACTION_COLORS[CREATED])

    def test_each_action_has_its_own_colour(self) -> None:
        self.assertEqual(len(set(ACTION_COLORS.values())), 3)

    def test_creation_card_lists_the_task_fields(self) -> None:
        card = self.creation()
        self.assertEqual(field_of(card, "Status")[1], "Ready")
        self.assertEqual(field_of(card, "Priority")[1], "High")
        self.assertEqual(field_of(card, "Assigned to")[1], "alice, bob")
        self.assertTrue(all(inline for _, _, inline in card.fields[:3]))

    def test_links_become_one_field_of_masked_links(self) -> None:
        card = self.creation()
        name, value, inline = field_of(card, "Links")
        self.assertIn("[GitHub](https://github.com/org/repo/issues/1)", value)
        self.assertIn("[Discord](https://discord.com/channels/1/2)", value)
        self.assertFalse(inline)
        # The card title points at the GitHub task.
        self.assertEqual(card.url, "https://github.com/org/repo/issues/1")

    def test_task_details_keep_the_internal_keys(self) -> None:
        details = task_details(
            {"title": "T", "status": "Backlog", "priority": "Low", "assignee": ""}
        )
        self.assertEqual(details, {"status": "Backlog", "priority": "Low", "assignee": ""})

    def test_an_empty_assignee_reads_as_unassigned(self) -> None:
        card = build_notification(
            CREATED,
            DISCORD_TO_GITHUB,
            "T",
            details={"status": "Backlog", "priority": "Low", "assignee": ""},
        )
        self.assertEqual(field_of(card, "Assigned to")[1], "Unassigned")

    def test_empty_links_and_details_are_dropped(self) -> None:
        card = build_notification(
            DELETED,
            GITHUB_TO_DISCORD,
            "Fix login",
            details={"status": ""},
            links={"GitHub": "", "Discord": "https://discord.com/channels/1/2"},
        )
        self.assertEqual(len(card.fields), 1)
        self.assertIn("Links", card.fields[0][0])
        self.assertNotIn("GitHub", card.fields[0][1])
        self.assertEqual(card.url, "")

    def test_a_task_without_title_still_reads(self) -> None:
        card = build_notification(DELETED, GITHUB_TO_DISCORD, "")
        self.assertIn("Untitled", card.title)
        self.assertEqual(card.fields, [])

    def test_changes_name_every_modified_field(self) -> None:
        before = ("Fix login", "Backlog", "Low", "alice")
        after = ("Fix logout", "In progress", "Low", "")
        self.assertEqual(
            state_changes(before, after),
            [
                ("title", "Fix login", "Fix logout"),
                ("status", "Backlog", "In progress"),
                ("assignee", "alice", ""),
            ],
        )

    def test_changes_are_written_out_on_the_card(self) -> None:
        changes = state_changes(
            ("Fix login", "Backlog", "Low", "alice"),
            ("Fix login", "In progress", "Low", ""),
        )
        card = build_notification(UPDATED, GITHUB_TO_DISCORD, "Fix login", changes=changes)
        name, value, inline = field_of(card, "Changes")
        self.assertIn("**Status**: Backlog → In progress", value)
        self.assertIn("**Assigned to**: alice → None", value)
        self.assertNotIn("Priority", value)
        self.assertFalse(inline)

    def test_a_long_change_list_stays_under_the_discord_limit(self) -> None:
        changes = [("title", "x" * 80, "y" * 80) for _ in range(20)]
        card = build_notification(UPDATED, GITHUB_TO_DISCORD, "T", changes=changes)
        self.assertLessEqual(len(field_of(card, "Changes")[1]), VALUE_LIMIT)

    def test_an_identical_state_has_no_changes(self) -> None:
        state = ("Fix login", "Backlog", "Low", "alice")
        self.assertEqual(state_changes(state, state), [])

    def test_changes_of_a_discord_thread_update(self) -> None:
        message = SimpleNamespace(
            author=SimpleNamespace(bot=False),
            content="Assigned to: alice\nDescription:\nFix login",
            jump_url="https://discord.com/channels/1/2/3",
        )
        before = discord_task_data(message, thread("Fix login", "Backlog", "Low"))
        after = discord_task_data(message, thread("Fix login", "Done", "Low"))
        self.assertEqual(
            state_changes(task_state(before), task_state(after)),
            [("status", "Backlog", "Done")],
        )

    def test_changes_of_a_github_item_update(self) -> None:
        def item(status: str) -> dict[str, object]:
            return {
                "content": {
                    "title": "Fix login",
                    "assignees": {"nodes": [{"login": "alice"}]},
                },
                "fieldValues": {
                    "nodes": [{"name": status, "field": {"name": "Status"}}]
                },
            }

        before = task_state(github_item_data(item("Backlog")))
        after = task_state(github_item_data(item("In review")))
        self.assertEqual(state_changes(before, after), [("status", "Backlog", "In review")])

    def test_plain_text_fallback_keeps_every_field(self) -> None:
        text = notification_text(self.creation())
        self.assertIn("Fix login", text)
        self.assertIn("Task created", text)
        self.assertIn("Ready", text)
        self.assertIn("alice, bob", text)
        self.assertIn("https://github.com/org/repo/issues/1", text)


class LanguageTests(unittest.TestCase):
    def test_ten_languages_share_the_same_keys(self) -> None:
        self.assertEqual(len(LANGUAGES), 10)
        keys = {frozenset(words) for words in LANGUAGES.values()}
        self.assertEqual(len(keys), 1, "a language is missing a key")

    def test_every_language_builds_a_complete_card(self) -> None:
        for code, words in LANGUAGES.items():
            with self.subTest(language=code):
                card = build_notification(
                    UPDATED,
                    GITHUB_TO_DISCORD,
                    "Fix login",
                    details={"status": "Done", "priority": "High", "assignee": ""},
                    changes=[("status", "Backlog", "Done"), ("assignee", "alice", "")],
                    links={"GitHub": "https://github.com/org/repo/issues/1"},
                    language=code,
                )
                self.assertIn(words["updated"], card.author)
                self.assertIn(words["status"], field_of(card, words["status"])[0])
                self.assertEqual(field_of(card, words["assignee"])[1], words["unassigned"])
                self.assertIn(words["none"], field_of(card, words["changes"])[1])
                self.assertLessEqual(len(card.title), 256)

    def test_a_card_speaks_the_chosen_language(self) -> None:
        card = build_notification(
            CREATED,
            DISCORD_TO_GITHUB,
            "Fix login",
            details={"status": "Ready", "priority": "High", "assignee": "alice"},
            language="fr",
        )
        self.assertIn("Tâche créée", card.author)
        self.assertIn("Statut", field_of(card, "Statut")[0])
        self.assertIn("Assigné à", field_of(card, "Assigné")[0])

    def test_an_unknown_language_falls_back_to_english(self) -> None:
        self.assertEqual(language_words("klingon"), LANGUAGES["en"])
        card = build_notification(CREATED, DISCORD_TO_GITHUB, "T", language="klingon")
        self.assertIn("Task created", card.author)

    def test_the_listing_marks_the_language_in_use(self) -> None:
        listing = language_listing("ru")
        self.assertIn("`ru`", listing)
        self.assertIn("Русский", listing)
        marked = [line for line in listing.splitlines() if line.startswith("**>**")]
        self.assertEqual(len(marked), 1)
        self.assertIn("`ru`", marked[0])


if __name__ == "__main__":
    unittest.main()
