"""The shared form card, driven without Discord.

These tests need discord.py, so the project `.venv`.
"""

import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

try:
    import discord
except ImportError:  # pragma: no cover
    raise unittest.SkipTest("discord.py is not installed")

from bot_horizon.config import Settings
from bot_horizon.form import (
    FORM_ID,
    PANEL_IDS,
    FieldModal,
    FormView,
    changed_fields,
    draft_of,
    month_days,
    shifted,
)
from bot_horizon.main import HorizonBot
from bot_horizon.notifications import language_words

FORM_CHANNEL = 400
USER_ID = 7
POST_URL = "https://discord.com/channels/1/200"
OPTIONS = {
    "statuses": ["Backlog", "Ready", "In progress", "In review", "Done"],
    "priorities": ["Low", "Medium", "High", "Urgent"],
    "sizes": ["S", "M", "L"],
    "assignees": ["alice", "bob"],
    "start_date": True,
    "target_date": True,
}
TASK = {
    "title": "Fix login",
    "description": "On mobile",
    "status": "Ready",
    "priority": "High",
    "size": "M",
    "assignees": ["alice"],
    "start_date": "2026-10-20",
    "target_date": "2026-10-31",
    "closed": False,
}


def fake_bot(**overrides) -> SimpleNamespace:
    bot = SimpleNamespace(
        words=language_words("en"),
        create_task_from_form=AsyncMock(),
        load_task_for_form=AsyncMock(return_value=("key", {"id": "item"}, dict(TASK))),
        save_task_from_form=AsyncMock(return_value=True),
        get_channel=lambda thread_id: None,
    )
    for key, value in overrides.items():
        setattr(bot, key, value)
    return bot


def post() -> discord.Thread:
    thread = object.__new__(discord.Thread)
    thread.id = 200
    thread.guild = SimpleNamespace(id=1)
    return thread


class FakeResponse:
    def __init__(self) -> None:
        self.edit_message = AsyncMock()
        self.defer = AsyncMock()
        self.send_modal = AsyncMock()


def click(user_id: int = USER_ID, name: str = "Alice") -> SimpleNamespace:
    """An interaction on the card, as a click or a choice in a menu."""
    return SimpleNamespace(
        response=FakeResponse(),
        user=SimpleNamespace(id=user_id, display_name=name),
        message=SimpleNamespace(edit=AsyncMock()),
    )


class CardTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bot = fake_bot()
        self.form = FormView(self.bot)
        self.form.options = dict(OPTIONS)
        self.form.message = SimpleNamespace(edit=AsyncMock())
        self.form.build()

    def item(self, name: str):
        return next(
            item for item in self.form.walk_children()
            if getattr(item, "custom_id", None) == f"{FORM_ID}{name}"
        )

    def text(self) -> str:
        return "\n".join(
            item.content for item in self.form.walk_children()
            if isinstance(item, discord.ui.TextDisplay)
        )

    async def press(self, name: str, interaction=None):
        interaction = interaction or click()
        await self.item(name).callback(interaction)
        return interaction

    async def pick(self, name: str, *values):
        self.item(name)._values = list(values)
        return await self.press(name)

    async def type_in(self, name: str, value: str) -> FieldModal:
        """Click the button of a text field, then type and send its dialog."""
        clicked = await self.press(name)
        dialog = clicked.response.send_modal.await_args.args[0]
        dialog.input._value = value
        await dialog.on_submit(click())
        return dialog


class LayoutTests(CardTestCase):
    async def test_the_whole_form_is_one_card(self) -> None:
        [card] = self.form.children
        self.assertIsInstance(card, discord.ui.Container)
        ids = {item.custom_id.removeprefix(FORM_ID) for item in self.form.walk_children() if getattr(item, "custom_id", None)}
        self.assertEqual(
            ids,
            {"post", "title", "description", "status", "priority", "size", "assignees",
             "start_date", "target_date", "submit", "cancel", "link"},
        )
        self.assertLessEqual(PANEL_IDS, {f"{FORM_ID}{name}" for name in ids})

    async def test_the_card_carries_no_icon(self) -> None:
        for item in self.form.walk_children():
            self.assertIsNone(getattr(item, "emoji", None))

    async def test_the_card_can_answer_after_a_restart(self) -> None:
        self.assertTrue(self.form.is_persistent())

    async def test_the_card_fits_the_discord_limits(self) -> None:
        self.form.options["assignees"] = [f"user{index}" for index in range(30)]
        self.form.draft["description"] = "x" * 5000
        self.form.notice = "Something happened"
        self.form.touched_by = "Alice"
        self.form.build()
        self.assertLessEqual(self.form.content_length(), 4000)
        self.assertLessEqual(len(list(self.form.walk_children())), 40)

    async def test_a_field_the_project_lacks_leaves_the_card(self) -> None:
        self.form.options = {**OPTIONS, "sizes": [], "assignees": [], "start_date": False, "target_date": False}
        self.form.build()
        ids = {getattr(item, "custom_id", None) for item in self.form.walk_children()}
        for name in ("size", "assignees", "start_date", "target_date"):
            self.assertNotIn(f"{FORM_ID}{name}", ids)


class PostTests(CardTestCase):
    async def test_picking_a_post_loads_its_task_into_the_card(self) -> None:
        thread = post()
        self.bot.get_channel = lambda thread_id: thread
        interaction = await self.pick("post", SimpleNamespace(id=200))
        interaction.response.defer.assert_awaited_once()
        self.assertIs(self.form.thread, thread)
        self.assertEqual(self.form.draft["title"], "Fix login")
        self.assertIn("Edit a task", self.text())
        self.assertEqual(self.item("submit").label, "Save")
        self.form.message.edit.assert_awaited()

    async def test_clearing_the_post_starts_a_new_task(self) -> None:
        self.form.thread, self.form.draft = post(), draft_of(TASK)
        await self.pick("post")
        self.assertIsNone(self.form.thread)
        self.assertEqual(self.form.draft["title"], "")
        self.assertEqual(self.item("submit").label, "Create")

    async def test_a_pasted_link_loads_its_post(self) -> None:
        thread = post()
        self.bot.get_channel = lambda thread_id: thread
        await self.type_in("link", POST_URL)
        self.assertIs(self.form.thread, thread)
        self.form.message.edit.assert_awaited()

    async def test_a_link_to_no_post_is_refused(self) -> None:
        await self.type_in("link", "not a link")
        self.assertIsNone(self.form.thread)
        self.assertIn("not in the task forum", self.text())


class TextTests(CardTestCase):
    async def test_a_field_button_opens_a_dialog_of_one_input_filled_with_its_value(self) -> None:
        self.form.draft["title"] = "Fix login"
        clicked = await self.press("title")
        dialog = clicked.response.send_modal.await_args.args[0]
        self.assertIsInstance(dialog, FieldModal)
        self.assertEqual(len(dialog.children), 1)
        self.assertEqual(dialog.input.default, "Fix login")

    async def test_the_dialog_fills_the_card(self) -> None:
        await self.type_in("description", "  On mobile too  ")
        self.assertEqual(self.form.draft["description"], "On mobile too")
        self.assertIn("On mobile too", self.text())

    async def test_a_title_is_cut_to_the_length_of_a_post_name(self) -> None:
        await self.type_in("title", "x" * 150)
        self.assertEqual(len(self.form.draft["title"]), 100)

    async def test_the_card_names_who_used_it_last(self) -> None:
        await self.pick("priority", "Urgent")
        self.assertEqual(self.form.draft["priority"], "Urgent")
        self.assertIn("Last change by Alice", self.text())

    async def test_the_assignees_come_from_one_menu(self) -> None:
        await self.pick("assignees", "alice", "bob")
        self.assertEqual(self.form.draft["assignees"], ["alice", "bob"])


class CalendarTests(CardTestCase):
    async def test_a_date_opens_the_calendar_of_its_month(self) -> None:
        self.form.draft["target_date"] = "2026-10-31"
        await self.press("target_date")
        self.assertEqual(self.form.picking, "target_date")
        self.assertIn("October 2026", self.text())
        first, second = self.item("days0"), self.item("days1")
        self.assertEqual(len(first.options) + len(second.options), 31)
        self.assertEqual(first.options[0].label, "Thu 1")
        self.assertTrue(second.options[-1].default)

    async def test_picking_a_day_sets_the_date_and_goes_back(self) -> None:
        await self.press("start_date")
        self.form.month = (2026, 10)
        self.form.build()
        await self.pick("days1", "2026-10-20")
        self.assertEqual(self.form.draft["start_date"], "2026-10-20")
        self.assertIsNone(self.form.picking)
        self.assertIn("<t:", self.text())

    async def test_the_months_turn(self) -> None:
        await self.press("start_date")
        self.form.month = (2026, 12)
        await self.press("next")
        self.assertEqual(self.form.month, (2027, 1))
        await self.press("previous")
        await self.press("previous")
        self.assertEqual(self.form.month, (2026, 11))

    async def test_a_start_after_the_target_is_refused(self) -> None:
        self.form.draft["target_date"] = "2026-10-10"
        await self.press("start_date")
        await self.pick("days1", "2026-10-20")
        self.assertEqual(self.form.draft["start_date"], "")
        self.assertEqual(self.form.picking, "start_date")
        self.assertIn("after the target date", self.text())

    async def test_a_date_can_be_cleared(self) -> None:
        self.form.draft["start_date"] = "2026-10-20"
        await self.press("start_date")
        await self.press("clear")
        self.assertEqual(self.form.draft["start_date"], "")

    async def test_the_calendar_knows_the_month_lengths(self) -> None:
        words = language_words("fr")
        self.assertEqual(len(month_days(2026, 2, words)), 28)
        self.assertEqual(len(month_days(2028, 2, words)), 29)
        self.assertEqual(month_days(2026, 10, words)[19], ("mar. 20", "2026-10-20"))
        self.assertEqual(shifted(2026, 1, -1), (2025, 12))


class SubmitTests(CardTestCase):
    async def test_a_task_without_title_asks_for_one(self) -> None:
        clicked = await self.press("submit")
        self.bot.create_task_from_form.assert_not_awaited()
        self.assertEqual(clicked.response.send_modal.await_args.args[0].field, "title")

    async def test_creating_resets_the_card_with_the_link(self) -> None:
        self.form.draft["title"] = "Ship it"
        self.bot.create_task_from_form.return_value = SimpleNamespace(jump_url=POST_URL)
        await self.press("submit")
        self.bot.create_task_from_form.assert_awaited_once()
        self.assertEqual(self.form.draft["title"], "")
        self.assertIn(POST_URL, self.text())

    async def test_a_failed_save_keeps_the_task(self) -> None:
        self.form.draft["title"] = "Ship it"
        self.bot.create_task_from_form.side_effect = RuntimeError("down")
        await self.press("submit")
        self.assertEqual(self.form.draft["title"], "Ship it")
        self.assertIn("did not work", self.text())

    async def test_only_what_the_user_changed_is_applied_to_the_task_as_it_is_now(self) -> None:
        self.form.thread = SimpleNamespace(jump_url=POST_URL)
        self.form.draft, self.form.base = draft_of(TASK), draft_of(TASK)
        # GitHub has moved the task to In review since it was loaded.
        self.bot.load_task_for_form.return_value = ("key", {"id": "item"}, {**TASK, "status": "In review"})
        self.form.draft["priority"] = "Urgent"
        await self.press("submit")
        *_, wanted = self.bot.save_task_from_form.await_args.args
        self.assertEqual((wanted["status"], wanted["priority"]), ("In review", "Urgent"))

    async def test_cancel_empties_the_card(self) -> None:
        self.form.draft["title"] = "Draft"
        await self.press("cancel")
        self.assertEqual(self.form.draft["title"], "")
        self.assertIn("Cancelled", self.text())


class ChannelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bot = HorizonBot(
            Settings(
                discord_token="t",
                discord_channel_id=100,
                github_token="g",
                github_project_id="p",
                discord_form_channel_id=FORM_CHANNEL,
            ),
            SimpleNamespace(),
        )
        self.bot.form_view = FormView(self.bot)

    async def test_the_card_follows_the_language_command(self) -> None:
        form = self.bot.form_view
        form.message = SimpleNamespace(edit=AsyncMock())
        command = SimpleNamespace(
            content="!lang fr",
            author=SimpleNamespace(bot=False, guild_permissions=SimpleNamespace(manage_guild=True)),
            channel=SimpleNamespace(id=500, send=AsyncMock()),
        )
        self.bot.settings = Settings(
            discord_token="t",
            discord_channel_id=100,
            github_token="g",
            github_project_id="p",
            discord_notification_channel_id=500,
            discord_form_channel_id=FORM_CHANNEL,
        )
        with patch("bot_horizon.main.LANGUAGE_FILE", SimpleNamespace(write_text=lambda *a, **k: None)):
            await self.bot.on_message(command)
        form.message.edit.assert_awaited_once()
        text = "\n".join(
            item.content for item in form.walk_children() if isinstance(item, discord.ui.TextDisplay)
        )
        self.assertIn("Nouvelle tâche", text)

    async def test_the_card_has_the_purple_of_horizon(self) -> None:
        [card] = self.bot.form_view.children
        self.assertEqual(int(card.accent_colour), 0x4C008E)

    async def test_the_card_of_a_previous_run_is_redrawn_and_old_panels_go(self) -> None:
        def posted(*ids: str) -> SimpleNamespace:
            return SimpleNamespace(
                author=self.bot.user,
                components=[SimpleNamespace(children=[SimpleNamespace(custom_id=i) for i in ids])],
                delete=AsyncMock(),
                edit=AsyncMock(),
            )

        card = posted(*PANEL_IDS)
        old = posted("horizon:panel:new", "horizon:panel:edit")
        channel = object.__new__(discord.TextChannel)
        channel.name = "create-task"

        async def history(limit=None):
            yield card
            yield old

        with patch.object(HorizonBot, "get_channel", return_value=channel), patch.object(
            discord.TextChannel, "history", lambda self, limit=None: history()
        ), patch.object(discord.TextChannel, "send", AsyncMock()) as send, patch.object(
            HorizonBot, "form_options", AsyncMock(return_value=OPTIONS)
        ):
            await self.bot.ensure_form_panel()
        send.assert_not_awaited()
        card.edit.assert_awaited_once()
        old.delete.assert_awaited_once()
        self.assertIs(self.bot.form_view.message, card)


class DraftTests(unittest.TestCase):
    def test_changed_fields_are_the_ones_that_differ(self) -> None:
        after = {**draft_of(TASK), "priority": "Low", "assignees": ["bob"]}
        self.assertEqual(changed_fields(draft_of(TASK), after), {"priority": "Low", "assignees": ["bob"]})

    def test_the_calendar_opens_on_this_month_for_an_empty_date(self) -> None:
        self.assertEqual(FormView(fake_bot()).month, (date.today().year, date.today().month))


if __name__ == "__main__":
    unittest.main()
