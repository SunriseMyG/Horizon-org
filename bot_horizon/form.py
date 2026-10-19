"""The task form: one card in a Discord channel, shared by the whole team.

The card is the form itself. At its top, a list of the forum posts: leave it
empty for a new task, or pick a post to load its task. Below, the whole task:
the title and the description with a button each, select menus for the
status, the priority, the size and the assignees, and the two dates, picked
on a calendar drawn in the same card. Everyone sees the same card, so it shows
the task of whoever used it last.

Discord has no text input inside a message: the title, the description and
a post link are typed in a small dialog of one input, opened by the button of
the field and filled with its value. Saving goes through `HorizonBot`, which
writes GitHub and the forum post together.
"""

import calendar
import logging
from datetime import date
from typing import Any

import discord

from .notifications import discord_date
from .sync import PRIORITY_NAMES, STATUS_NAMES, discord_thread_id


logger = logging.getLogger(__name__)

FORM_ID = "horizon:card:"
PANEL_IDS = frozenset({f"{FORM_ID}post", f"{FORM_ID}submit"})
# The panels of earlier versions, replaced when the bot starts.
OLD_PANEL_IDS = frozenset(
    {
        "horizon:task:new",
        "horizon:task:edit",
        "horizon:form:new",
        "horizon:form:edit",
        "horizon:panel:new",
        "horizon:panel:edit",
    }
)
# A select menu holds at most 25 options.
SELECT_LIMIT = 25
# A card shows at most 4000 characters: the description gets what is left.
DESCRIPTION_SHOWN = 1500
# The purple of Horizon.
FORM_COLOUR = 0x4C008E
DATE_KEYS = ("start_date", "target_date")
DRAFT_KEYS = (
    "title",
    "description",
    "status",
    "priority",
    "size",
    "assignees",
    "start_date",
    "target_date",
)
DEFAULT_OPTIONS = {
    "statuses": list(STATUS_NAMES),
    "priorities": list(PRIORITY_NAMES),
    "sizes": [],
    "assignees": [],
    "start_date": False,
    "target_date": False,
}


class TaskError(Exception):
    """A form action refused, named by the key of its message in `languages.json`."""

    def __init__(self, code: str, **values: str) -> None:
        super().__init__(code)
        self.code = code
        self.values = values

    def text(self, words: dict[str, str]) -> str:
        return words[self.code].format(**self.values)


def new_draft() -> dict[str, Any]:
    return {
        "title": "",
        "description": "",
        "status": STATUS_NAMES[0],
        "priority": PRIORITY_NAMES[0],
        "size": "",
        "assignees": [],
        "start_date": "",
        "target_date": "",
    }


def draft_of(details: dict[str, Any]) -> dict[str, Any]:
    """The editable fields of a task, as the form holds them."""
    draft = {key: details.get(key) or "" for key in DRAFT_KEYS}
    draft["assignees"] = list(details.get("assignees") or [])
    return draft


def changed_fields(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """The fields a user changed in the form, from what it was opened with."""
    return {key: after[key] for key in DRAFT_KEYS if after[key] != before[key]}


def panel_ids(message: discord.Message) -> set[str]:
    """The custom ids of the components a message carries, however nested."""
    ids: set[str] = set()
    stack = list(message.components)
    while stack:
        component = stack.pop()
        if custom_id := getattr(component, "custom_id", None):
            ids.add(custom_id)
        stack.extend(getattr(component, "children", None) or [])
        if (accessory := getattr(component, "accessory", None)) is not None:
            stack.append(accessory)
    return ids


def month_days(year: int, month: int, words: dict[str, str]) -> list[tuple[str, str]]:
    """The (label, ISO date) of every day of a month, with its weekday."""
    weekdays = words["weekdays"].split(",")
    return [
        (f"{weekdays[date(year, month, day).weekday()]} {day}", date(year, month, day).isoformat())
        for day in range(1, calendar.monthrange(year, month)[1] + 1)
    ]


def shifted(year: int, month: int, step: int) -> tuple[int, int]:
    index = year * 12 + month - 1 + step
    return index // 12, index % 12 + 1


async def find_thread(bot: Any, thread_id: int | None) -> discord.Thread:
    if thread_id is None:
        raise TaskError("not_a_task")
    thread = bot.get_channel(thread_id)
    if thread is None:
        try:
            thread = await bot.fetch_channel(thread_id)
        except discord.HTTPException:
            raise TaskError("not_a_task") from None
    if not isinstance(thread, discord.Thread):
        raise TaskError("not_a_task")
    return thread


class FormView(discord.ui.LayoutView):
    """The shared form card, alive across restarts.

    Every component carries a fixed custom id, so the card answers again once
    the bot restarts; it then comes back as an empty new task.
    """

    def __init__(self, bot: Any) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.options: dict[str, Any] = dict(DEFAULT_OPTIONS)
        # The message of the card, edited after a message typed in the channel.
        self.message: discord.Message | None = None
        self.reset()

    def reset(self, notice: str | None = None) -> None:
        """Back to an empty new task."""
        self.draft = new_draft()
        # The task as the form loaded it, to tell what the user changed.
        self.base = dict(self.draft)
        # The post being edited; None for a new task.
        self.thread: discord.Thread | None = None
        # The date being picked on the calendar, and the month it shows.
        self.picking: str | None = None
        self.month = (date.today().year, date.today().month)
        self.notice = notice
        self.touched_by: str | None = None
        self.build()

    # --- layout -------------------------------------------------------------

    def build(self) -> None:
        """Lay the card out again, with the current values."""
        self.clear_items()
        form = discord.ui.Container(accent_colour=FORM_COLOUR)
        if self.picking:
            self.build_calendar(form)
        else:
            self.build_form(form)
        self.add_item(form)

    def build_form(self, form: discord.ui.Container) -> None:
        words = self.bot.words
        heading = f"## {words['edit_task'] if self.thread else words['new_task']}"
        if self.thread is not None:
            heading += f"\n{self.thread.jump_url}"
        form.add_item(discord.ui.TextDisplay(heading))
        posts = discord.ui.ChannelSelect(
            custom_id=f"{FORM_ID}post",
            channel_types=[discord.ChannelType.public_thread],
            placeholder=words["edit_post"],
            min_values=0,
            default_values=[self.thread] if self.thread is not None else [],
        )
        posts.callback = self.pick_post(posts)
        form.add_item(discord.ui.ActionRow(posts))
        form.add_item(discord.ui.Separator())
        form.add_item(
            self.field("title", words["title"], self.draft["title"] or f"*{words['untitled']}*")
        )
        description = self.draft["description"] or f"*{words['no_description']}*"
        if len(description) > DESCRIPTION_SHOWN:
            description = description[: DESCRIPTION_SHOWN - 1] + "\N{HORIZONTAL ELLIPSIS}"
        form.add_item(self.field("description", words["description"], description))
        form.add_item(discord.ui.Separator())
        self.add_select(form, "status", self.options["statuses"])
        self.add_select(form, "priority", self.options["priorities"])
        if self.options["sizes"]:
            self.add_select(form, "size", self.options["sizes"], optional=True)
        logins = list(dict.fromkeys(self.draft["assignees"] + self.options["assignees"]))
        if logins := logins[:SELECT_LIMIT]:
            select = discord.ui.Select(
                custom_id=f"{FORM_ID}assignees",
                placeholder=words["assign_to"],
                min_values=0,
                max_values=len(logins),
                options=[
                    discord.SelectOption(
                        label=login, default=login in self.draft["assignees"]
                    )
                    for login in logins
                ],
            )
            select.callback = self.choose("assignees", select, many=True)
            form.add_item(discord.ui.TextDisplay(f"**{words['assignee']}**"))
            form.add_item(discord.ui.ActionRow(select))
        dates = [key for key in DATE_KEYS if self.options[key]]
        if dates:
            form.add_item(discord.ui.Separator())
        for key in dates:
            form.add_item(
                discord.ui.Section(
                    discord.ui.TextDisplay(
                        f"**{words[key]}**\n{discord_date(self.draft[key]) or words['none']}"
                    ),
                    accessory=self.button(words["pick"], key, self.open_calendar(key)),
                )
            )
        form.add_item(discord.ui.Separator())
        if footer := self.footer():
            form.add_item(discord.ui.TextDisplay(footer))
        form.add_item(
            discord.ui.ActionRow(
                self.button(
                    words["save"] if self.thread else words["create"],
                    "submit",
                    self.submit,
                    discord.ButtonStyle.success,
                ),
                self.button(words["cancel"], "cancel", self.cancel, discord.ButtonStyle.secondary),
                self.button(words["paste_link"], "link", self.ask("link")),
            )
        )

    def build_calendar(self, form: discord.ui.Container) -> None:
        """The month of the date being picked, a day of it to choose."""
        words = self.bot.words
        year, month = self.month
        months = words["months"].split(",")
        form.add_item(
            discord.ui.TextDisplay(
                f"## {words[self.picking]}\n### {months[month - 1]} {year}"
            )
        )
        form.add_item(
            discord.ui.ActionRow(
                self.button(words["previous_month"], "previous", self.turn(-1), discord.ButtonStyle.secondary),
                self.button(words["next_month"], "next", self.turn(1), discord.ButtonStyle.secondary),
                self.button(words["clear"], "clear", self.set_date(""), discord.ButtonStyle.secondary),
                self.button(words["back"], "back", self.close_calendar, discord.ButtonStyle.secondary),
            )
        )
        days = month_days(year, month, words)
        for part, chunk in enumerate((days[:16], days[16:])):
            select = discord.ui.Select(
                custom_id=f"{FORM_ID}days{part}",
                placeholder=f"{words['day']} {chunk[0][0].split()[-1]}\N{EN DASH}{chunk[-1][0].split()[-1]}",
                options=[
                    discord.SelectOption(
                        label=label, value=value, default=value == self.draft[self.picking]
                    )
                    for label, value in chunk
                ],
            )
            select.callback = self.pick_day(select)
            form.add_item(discord.ui.ActionRow(select))
        if self.notice:
            form.add_item(discord.ui.TextDisplay(self.notice))

    def field(self, key: str, name: str, value: str) -> discord.ui.Section:
        """A line of the card with the button that asks for its text."""
        return discord.ui.Section(
            discord.ui.TextDisplay(f"**{name}**\n{value}"),
            accessory=self.button(self.bot.words["change"], key, self.ask(key)),
        )

    def button(
        self,
        label: str,
        name: str,
        callback: Any,
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
    ) -> discord.ui.Button:
        item = discord.ui.Button(label=label, style=style, custom_id=f"{FORM_ID}{name}")
        item.callback = callback
        return item

    def add_select(
        self,
        form: discord.ui.Container,
        field: str,
        names: list[str],
        optional: bool = False,
    ) -> None:
        select = discord.ui.Select(
            custom_id=f"{FORM_ID}{field}",
            placeholder=self.bot.words[field],
            min_values=0 if optional else 1,
            max_values=1,
            options=[
                discord.SelectOption(label=name, default=name == self.draft[field])
                for name in names[:SELECT_LIMIT]
            ],
        )
        select.callback = self.choose(field, select)
        form.add_item(discord.ui.TextDisplay(f"**{self.bot.words[field]}**"))
        form.add_item(discord.ui.ActionRow(select))

    def footer(self) -> str | None:
        """What happened, and who used the card last."""
        words = self.bot.words
        lines = [self.notice] if self.notice else []
        if self.touched_by:
            lines.append(f"-# {words['touched_by'].format(name=self.touched_by)}")
        return "\n".join(lines) or None

    # --- showing the card ---------------------------------------------------

    def touch(self, interaction: discord.Interaction) -> None:
        self.touched_by = interaction.user.display_name
        if interaction.message is not None:
            self.message = interaction.message

    async def show(self, interaction: discord.Interaction) -> None:
        """Redraw the card, in answer to a click on it."""
        self.touch(interaction)
        self.build()
        await interaction.response.edit_message(view=self)

    async def redraw(self) -> None:
        """Redraw the card outside of a click: after a post is loaded, a save."""
        self.build()
        if self.message is not None:
            await self.message.edit(view=self, allowed_mentions=discord.AllowedMentions.none())

    # --- user actions -------------------------------------------------------

    def choose(self, field: str, select: discord.ui.Select, many: bool = False) -> Any:
        async def callback(interaction: discord.Interaction) -> None:
            values = list(select.values)
            self.draft[field] = values if many else (values[0] if values else "")
            self.notice = None
            await self.show(interaction)

        return callback

    def ask(self, field: str) -> Any:
        """Open the dialog of a text field."""

        async def callback(interaction: discord.Interaction) -> None:
            await interaction.response.send_modal(FieldModal(self, field))

        return callback

    async def write(self, interaction: discord.Interaction, field: str, value: str) -> None:
        """Put what was typed in a dialog into the card."""
        self.touch(interaction)
        self.notice = None
        if field == "link":
            # Reading the task may outlast the 3 seconds Discord waits.
            await interaction.response.defer()
            await self.load(discord_thread_id(value))
            await self.redraw()
            return
        self.draft[field] = value[:100] if field == "title" else value
        await self.show(interaction)

    def pick_post(self, select: discord.ui.ChannelSelect) -> Any:
        async def callback(interaction: discord.Interaction) -> None:
            self.touch(interaction)
            if not select.values:
                self.reset()
                self.touch(interaction)
                await self.show(interaction)
                return
            # Reading the task may outlast the 3 seconds Discord waits.
            await interaction.response.defer()
            await self.load(select.values[0].id)
            await self.redraw()

        return callback

    async def load(self, thread_id: int | None) -> None:
        """Fill the card with the task of a post, as GitHub has it now."""
        try:
            thread = await find_thread(self.bot, thread_id)
            _, _, details = await self.bot.load_task_for_form(thread)
        except TaskError as error:
            self.notice = error.text(self.bot.words)
            return
        except Exception:
            logger.exception("Could not load a task into the form")
            self.notice = self.bot.words["failed"]
            return
        self.thread = thread
        self.draft = draft_of(details)
        self.base = dict(self.draft)
        self.notice = None

    def open_calendar(self, key: str) -> Any:
        async def callback(interaction: discord.Interaction) -> None:
            self.picking = key
            self.notice = None
            value = self.draft[key] or self.draft["start_date"] or self.draft["target_date"]
            day = date.fromisoformat(value) if value else date.today()
            self.month = (day.year, day.month)
            await self.show(interaction)

        return callback

    def turn(self, step: int) -> Any:
        async def callback(interaction: discord.Interaction) -> None:
            self.month = shifted(*self.month, step)
            await self.show(interaction)

        return callback

    def pick_day(self, select: discord.ui.Select) -> Any:
        async def callback(interaction: discord.Interaction) -> None:
            await self.set_date(select.values[0])(interaction)

        return callback

    def set_date(self, value: str) -> Any:
        async def callback(interaction: discord.Interaction) -> None:
            dates = {**self.draft, self.picking: value}
            if dates["start_date"] and dates["target_date"] and (
                dates["start_date"] > dates["target_date"]
            ):
                self.notice = self.bot.words["dates_order"]
                await self.show(interaction)
                return
            self.draft[self.picking] = value
            self.picking = None
            self.notice = None
            await self.show(interaction)

        return callback

    async def close_calendar(self, interaction: discord.Interaction) -> None:
        self.picking = None
        self.notice = None
        await self.show(interaction)

    async def submit(self, interaction: discord.Interaction) -> None:
        words = self.bot.words
        if not self.draft["title"]:
            # A task needs a title: ask for it right away.
            await interaction.response.send_modal(FieldModal(self, "title"))
            return
        self.touch(interaction)
        await interaction.response.defer()
        try:
            if self.thread is None:
                thread = await self.bot.create_task_from_form(self.draft)
                text = words["task_created"].format(link=thread.jump_url)
            else:
                text = await self.save()
        except TaskError as error:
            self.notice = error.text(words)
        except Exception:
            logger.exception("Could not save a task from the form")
            self.notice = words["failed"]
        else:
            touched_by = self.touched_by
            self.reset(text)
            self.touched_by = touched_by
        await self.redraw()

    async def save(self) -> str:
        """Apply the fields the user changed to the task as GitHub has it now.

        The card may have stayed on a task a while: a field the user left
        alone must not undo a change made since.
        """
        words = self.bot.words
        key, item, loaded = await self.bot.load_task_for_form(self.thread)
        wanted = {**draft_of(loaded), **changed_fields(self.base, self.draft)}
        changed = await self.bot.save_task_from_form(self.thread, key, item, loaded, wanted)
        if not changed:
            return words["nothing_changed"]
        return words["task_saved"].format(link=self.thread.jump_url)

    async def cancel(self, interaction: discord.Interaction) -> None:
        self.reset(self.bot.words["cancelled"])
        await self.show(interaction)


class FieldModal(discord.ui.Modal):
    """A dialog of one input: the title, the description or a post link."""

    def __init__(self, form: FormView, field: str) -> None:
        words = form.bot.words
        name = words["paste_link"] if field == "link" else words[field]
        super().__init__(title=name[:45])
        self.form = form
        self.field = field
        if field == "title":
            self.input = discord.ui.TextInput(
                default=form.draft["title"] or None, max_length=100
            )
        elif field == "description":
            self.input = discord.ui.TextInput(
                style=discord.TextStyle.paragraph,
                default=form.draft["description"][:4000] or None,
                required=False,
                max_length=4000,
            )
        else:
            self.input = discord.ui.TextInput(
                placeholder="https://discord.com/channels/\N{HORIZONTAL ELLIPSIS}"
            )
        self.add_item(discord.ui.Label(text=name[:45], component=self.input))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.form.write(interaction, self.field, self.input.value.strip())
