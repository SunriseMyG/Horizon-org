"""Contents of the cards posted to the notification channel.

Every synchronised action (creation, update, deletion, message) produces a card in the
`DISCORD_NOTIFICATION_CHANNEL_ID` channel. This module describes a card without
depending on Discord, so that it stays testable: `main` turns a `Notification`
into a `discord.Embed`, and `notification_text` into the plain text fallback
used when the bot may not send an embed.

The wording of a card comes from `languages.json`, picked by the language code
the notification channel is set to. Everything travelling through this module
is keyed by the internal names in `STATE_KEYS`, never by a translated label.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
import json
from pathlib import Path

CREATED = "created"
UPDATED = "updated"
DELETED = "deleted"
COMMENTED = "commented"

DISCORD_TO_GITHUB = "Discord \N{RIGHTWARDS ARROW} GitHub"
GITHUB_TO_DISCORD = "GitHub \N{RIGHTWARDS ARROW} Discord"

LANGUAGES: dict[str, dict[str, str]] = json.loads(
    Path(__file__).with_name("languages.json").read_text(encoding="utf-8")
)
DEFAULT_LANGUAGE = "en"

ACTION_ICONS = {
    CREATED: "\N{LARGE GREEN CIRCLE}",
    UPDATED: "\N{PENCIL}\N{VARIATION SELECTOR-16}",
    DELETED: "\N{WASTEBASKET}\N{VARIATION SELECTOR-16}",
    COMMENTED: "\N{SPEECH BALLOON}",
}
ACTION_COLORS = {
    CREATED: 0x57F287,
    UPDATED: 0x5865F2,
    DELETED: 0xED4245,
    COMMENTED: 0xFEE75C,
}

STATE_KEYS = (
    "title",
    "status",
    "priority",
    "assignee",
    "size",
    "start_date",
    "target_date",
)
DETAIL_KEYS = ("status", "priority", "assignee", "size", "start_date", "target_date")
DATE_KEYS = frozenset({"start_date", "target_date"})
DETAIL_ICONS = {
    "status": "\N{CHART WITH UPWARDS TREND}",
    "priority": "\N{SMALL ORANGE DIAMOND}",
    "assignee": "\N{BUST IN SILHOUETTE}",
    "size": "\N{STRAIGHT RULER}",
    "start_date": "\N{CALENDAR}",
    "target_date": "\N{CHEQUERED FLAG}",
}
CHANGES_ICON = "\N{MEMO}"
AUTHOR_ICON = "\N{BUST IN SILHOUETTE}"
MESSAGE_ICON = "\N{SPEECH BALLOON}"
LINKS_ICON = "\N{LINK SYMBOL}"

TITLE_LIMIT = 256
POST_LIMIT = 2000
VALUE_LIMIT = 1024
# A card quotes the start of a message; the link leads to the rest.
EXCERPT_LIMIT = 300


@dataclass(frozen=True)
class Notification:
    """A card: its header, its fields and its colour."""

    author: str
    title: str
    color: int
    url: str = ""
    fields: list[tuple[str, str, bool]] = field(default_factory=list)


def language_words(language: str) -> dict[str, str]:
    """The labels of a language, falling back to English for an unknown one."""
    return LANGUAGES.get(language, LANGUAGES[DEFAULT_LANGUAGE])


def state_changes(
    before: tuple[str, ...], after: tuple[str, ...]
) -> list[tuple[str, str, str]]:
    """The (field, before, after) triples between two task states."""
    return [
        (key, old, new)
        for key, old, new in zip(STATE_KEYS, before, after)
        if old != new
    ]


def task_details(task: dict[str, str]) -> dict[str, str]:
    """The task fields shown in a creation notification, by internal key."""
    return {key: task.get(key, "") for key in DETAIL_KEYS}


def discord_date(value: str) -> str:
    """An ISO date as a Discord timestamp, shown in the date format of each reader.

    Noon UTC keeps the same calendar day in every time zone a team works from.
    """
    if not value:
        return ""
    moment = datetime.combine(date.fromisoformat(value), time(12), timezone.utc)
    return f"<t:{int(moment.timestamp())}:D>"


def shown(key: str, value: str) -> str:
    """A task field the way a card writes it."""
    return discord_date(value) if key in DATE_KEYS else value


def post_card(details: dict, language: str = DEFAULT_LANGUAGE) -> str:
    """The opening message of a post the bot owns: the task, one field a line."""
    words = language_words(language)
    lines = [
        f"**{words[key]}:** {shown(key, details.get(key) or '')}"
        for key in ("status", "priority", "size")
        if details.get(key)
    ]
    lines.append(f"**{words['assignee']}:** {details.get('assignee') or words['unassigned']}")
    lines.extend(
        f"**{words[key]}:** {shown(key, details[key])}"
        for key in ("start_date", "target_date")
        if details.get(key)
    )
    footer = f"\n\n**{words['link']}:** <{details['url']}>" if details.get("url") else ""
    head = "\n".join(lines) + f"\n\n**{words['description']}:**\n"
    description = (details.get("description") or "").strip() or words["no_description"]
    room = POST_LIMIT - len(head) - len(footer)
    if len(description) > room:
        description = description[: room - 1].rstrip() + "\N{HORIZONTAL ELLIPSIS}"
    return head + description + footer


def excerpt(text: str) -> str:
    """The start of a message, cut on the excerpt limit."""
    text = text.strip()
    if len(text) <= EXCERPT_LIMIT:
        return text
    return text[: EXCERPT_LIMIT - 1].rstrip() + "\N{HORIZONTAL ELLIPSIS}"


def build_notification(
    action: str,
    direction: str,
    title: str,
    *,
    details: dict[str, str] | None = None,
    changes: list[tuple[str, str, str]] | None = None,
    message: tuple[str, str] | None = None,
    links: dict[str, str] | None = None,
    language: str = DEFAULT_LANGUAGE,
) -> Notification:
    """The card of an action, with its fields and its links.

    `message` is the (author, text) of a message posted on a task. Empty
    details and links are dropped rather than published empty, which the
    Discord API refuses anyway for the value of a field.
    """
    words = language_words(language)
    links = {name: url for name, url in (links or {}).items() if url}
    card_fields = []
    for key, value in (details or {}).items():
        if key == "assignee":
            value = value or words["unassigned"]
        if not value:
            continue
        card_fields.append((f"{DETAIL_ICONS[key]} {words[key]}", shown(key, value), True))
    if changes:
        lines = "\n".join(
            f"\N{BULLET} **{words[key]}**: "
            f"{shown(key, old) or words['none']} \N{RIGHTWARDS ARROW} "
            f"{shown(key, new) or words['none']}"
            for key, old, new in changes
        )
        card_fields.append((f"{CHANGES_ICON} {words['changes']}", lines[:VALUE_LIMIT], False))
    if message:
        author, text = message
        if author:
            card_fields.append((f"{AUTHOR_ICON} {words['author']}", author, True))
        if text := excerpt(text):
            card_fields.append((f"{MESSAGE_ICON} {words['message']}", text, False))
    if links:
        card_fields.append(
            (
                f"{LINKS_ICON} {words['links']}",
                " \N{BULLET} ".join(f"[{name}]({url})" for name, url in links.items()),
                False,
            )
        )
    return Notification(
        author=f"{words[action]} \N{BULLET} {direction}",
        title=f"{ACTION_ICONS[action]} {title or words['untitled']}"[:TITLE_LIMIT],
        color=ACTION_COLORS[action],
        url=links.get("GitHub", ""),
        fields=card_fields,
    )


def notification_text(notification: Notification) -> str:
    """The card as plain text, for a channel where the embed is refused."""
    lines = [f"**{notification.title}** \N{BULLET} {notification.author}"]
    lines.extend(
        f"**{name}:** {value}" for name, value, inline in notification.fields if inline
    )
    lines.extend(
        f"**{name}**\n{value}"
        for name, value, inline in notification.fields
        if not inline
    )
    return "\n".join(lines)


def language_listing(current: str) -> str:
    """The languages on offer, marking the one in use."""
    return "\n".join(
        f"{'**>**' if code == current else '  '} `{code}` \N{EM DASH} {words['name']}"
        for code, words in LANGUAGES.items()
    )
