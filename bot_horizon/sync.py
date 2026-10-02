from typing import Any
from urllib.parse import urlparse
import re


DISCORD_MARKER = "<!-- horizon-discord -->"
GITHUB_MARKER = "<!-- horizon-github -->"
STATUS_NAMES = ("Backlog", "Ready", "In progress", "In review", "Done")
PRIORITY_NAMES = ("Low", "Medium", "High", "Urgent")


def github_item_key(item: dict[str, Any]) -> str | None:
    content = item.get("content") or {}
    return content.get("url") or item.get("id")


def discord_message_body(message: Any) -> str:
    task = discord_task_data(message)
    return (
        f"{GITHUB_MARKER}\n"
        f"Status: {task['status']}\n"
        f"Priority: {task['priority']}\n"
        f"Assigned to: {task['assignee'] or 'Unassigned'}\n\n"
        f"Description:\n{task['description']}\n\n"
        f"Source: {message.jump_url}"
    )


def discord_task_data(message: Any, channel_override: Any = None) -> dict[str, str]:
    content = message.content.strip()
    channel = channel_override or getattr(message, "channel", None)
    tags = {
        tag.name.casefold(): tag.name
        for tag in getattr(channel, "applied_tags", [])
    }
    status = next(
        (name for name in STATUS_NAMES if name.casefold() in tags), STATUS_NAMES[0]
    )
    priority = next(
        (name for name in PRIORITY_NAMES if name.casefold() in tags), PRIORITY_NAMES[0]
    )
    assignee_match = re.search(r"^\s*Assigned to\s*:\s*(.+?)\s*$", content, re.MULTILINE | re.IGNORECASE)
    description_match = re.search(
        r"^\s*Description\s*:\s*(.*)$", content, re.MULTILINE | re.IGNORECASE | re.DOTALL
    )
    description = description_match.group(1).strip() if description_match else content
    assignee = assignee_match.group(1).strip() if assignee_match else ""
    if assignee.casefold() == "unassigned":
        assignee = ""
    title = getattr(channel, "name", "") or content.splitlines()[0][:120]
    return {
        "title": title[:120],
        "status": status,
        "priority": priority,
        "assignee": assignee,
        "description": description,
    }


def github_item_has_discord_marker(item: dict[str, Any]) -> bool:
    content = item.get("content") or {}
    return GITHUB_MARKER in (content.get("body") or "")


def github_item_source_url(item: dict[str, Any]) -> str | None:
    body = (item.get("content") or {}).get("body") or ""
    match = re.search(r"^Source:\s*(\S+)", body, re.MULTILINE)
    return match.group(1) if match else None


def discord_thread_id(source_url: str | None) -> int | None:
    """Extract the Discord thread id from a thread or message jump url."""
    if not source_url:
        return None
    parts = urlparse(source_url).path.strip("/").split("/")
    if len(parts) < 3 or parts[0] != "channels":
        return None
    try:
        return int(parts[2])
    except ValueError:
        return None


def github_item_data(
    item: dict[str, Any],
    assignee_field_name: str = "Owner",
    priority_field_name: str = "Task Priority",
) -> dict[str, str]:
    content = item.get("content") or {}
    fields = item.get("fieldValues", {}).get("nodes", [])
    status = next(
        (
            field["name"]
            for field in fields
            if field.get("field", {}).get("name") == "Status" and field.get("name")
        ),
        STATUS_NAMES[0],
    )
    field_values = item.get("fieldValues", {}).get("nodes", [])
    labels = content.get("labels", {}).get("nodes", [])
    priority_labels = {name.casefold() for name in PRIORITY_NAMES}
    priority = next(
        (
            label["name"]
            for label in labels
            if label.get("name", "").casefold() in priority_labels
        ),
        next(
            (
                field["name"]
                for field in field_values
                if field.get("field", {}).get("name") == priority_field_name
                and field.get("name")
            ),
            PRIORITY_NAMES[0],
        ),
    )
    assignees = content.get("assignees", {}).get("nodes", [])
    assignee = assignees[0].get("login", "") if assignees else next(
        (
            field.get("text", "")
            for field in field_values
            if field.get("field", {}).get("name") in {assignee_field_name, "Assigned to"}
        ),
        "",
    )
    description = content.get("body") or ""
    return {
        "title": content.get("title", "Nouvelle tache"),
        "url": content.get("url", ""),
        "status": status,
        "priority": priority,
        "assignee": assignee,
        "description": description,
    }


def is_bot_message(message: Any) -> bool:
    return bool(message.author.bot)
