"""Contents of the cards posted to the notification channel.

Every synchronised action (creation, update, deletion) produces a card in the
`DISCORD_NOTIFICATION_CHANNEL_ID` channel. This module describes a card without
depending on Discord, so that it stays testable: `main` turns a `Notification`
into a `discord.Embed`, and `notification_text` into the plain text fallback
used when the bot may not send an embed.

The wording of a card comes from `languages.json`, picked by the language code
the notification channel is set to. Everything travelling through this module
is keyed by the internal names in `STATE_KEYS`, never by a translated label.
"""

from dataclasses import dataclass, field
import json
from pathlib import Path

CREATED = "created"
UPDATED = "updated"
DELETED = "deleted"

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
}
ACTION_COLORS = {CREATED: 0x57F287, UPDATED: 0x5865F2, DELETED: 0xED4245}

STATE_KEYS = ("title", "status", "priority", "assignee")
DETAIL_KEYS = ("status", "priority", "assignee")
DETAIL_ICONS = {
    "status": "\N{CHART WITH UPWARDS TREND}",
    "priority": "\N{SMALL ORANGE DIAMOND}",
    "assignee": "\N{BUST IN SILHOUETTE}",
}
CHANGES_ICON = "\N{MEMO}"
LINKS_ICON = "\N{LINK SYMBOL}"

TITLE_LIMIT = 256
VALUE_LIMIT = 1024


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


def build_notification(
    action: str,
    direction: str,
    title: str,
    *,
    details: dict[str, str] | None = None,
    changes: list[tuple[str, str, str]] | None = None,
    links: dict[str, str] | None = None,
    language: str = DEFAULT_LANGUAGE,
) -> Notification:
    """The card of an action, with its fields and its links.

    Empty details and links are dropped rather than published empty, which the
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
        card_fields.append((f"{DETAIL_ICONS[key]} {words[key]}", value, True))
    if changes:
        lines = "\n".join(
            f"\N{BULLET} **{words[key]}**: "
            f"{old or words['none']} \N{RIGHTWARDS ARROW} {new or words['none']}"
            for key, old, new in changes
        )
        card_fields.append((f"{CHANGES_ICON} {words['changes']}", lines[:VALUE_LIMIT], False))
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
