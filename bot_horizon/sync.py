from typing import Any
from urllib.parse import urlparse
import re


DISCORD_MARKER = "<!-- horizon-discord -->"
GITHUB_MARKER = "<!-- horizon-github -->"
STATUS_NAMES = ("Backlog", "Ready", "In progress", "In review", "Done")
PRIORITY_NAMES = ("Low", "Medium", "High", "Urgent")
# A closed issue is a Done task, and a reopened one goes back to work.
DONE_STATUS = "Done"
REOPENED_STATUS = "In progress"
DISCORD_MESSAGE_LIMIT = 2000


def github_item_key(item: dict[str, Any]) -> str | None:
    content = item.get("content") or {}
    return content.get("url") or item.get("id")


def issue_body(description: str, source_url: str = "") -> str:
    """The body the bot writes on an issue that came from Discord."""
    body = f"{GITHUB_MARKER}\n{description.strip()}"
    return f"{body}\n\nSource: {source_url}" if source_url else body


def with_description(body: str, description: str) -> str:
    """An issue body with its description replaced, keeping its marker and link."""
    source_match = re.search(r"^Source:\s*(\S+)", body or "", re.MULTILINE)
    description = description.strip()
    if GITHUB_MARKER in (body or ""):
        description = f"{GITHUB_MARKER}\n{description}"
    if source_match:
        return f"{description}\n\nSource: {source_match.group(1)}"
    return description


def issue_description(body: str) -> str:
    """The description of an issue, without what the bot keeps in its body.

    Bodies written by older versions of the bot carry `Status:`, `Priority:`
    and `Assigned to:` lines before a `Description:` header.
    """
    body = (body or "").replace(GITHUB_MARKER, "")
    match = re.search(r"^\s*Description\s*:[ \t]*\n?", body, re.MULTILINE | re.IGNORECASE)
    if match:
        body = body[match.end() :]
    body = re.sub(r"^Source:\s*\S+\s*$", "", body, flags=re.MULTILINE)
    return body.strip()


def discord_message_body(message: Any) -> str:
    return issue_body(discord_task_data(message)["description"], message.jump_url)


def parse_assignees(text: str) -> list[str]:
    """Split an `Assigned to` value into GitHub logins.

    Logins may be separated by a comma, a semicolon, a slash or spaces, and an
    optional `@` prefix is accepted. Duplicates and `Unassigned` are dropped.
    """
    logins: list[str] = []
    seen: set[str] = set()
    for raw in re.split(r"[,;/]|\s+", text or ""):
        login = raw.strip().lstrip("@").strip()
        if not login or login.casefold() == "unassigned":
            continue
        if login.casefold() not in seen:
            seen.add(login.casefold())
            logins.append(login)
    return logins


def post_tags(details: dict[str, Any]) -> tuple[str, str]:
    """The forum tags a task carries: its status and its priority."""
    return details["status"], details["priority"]


def thread_task(thread: Any) -> dict[str, str]:
    """What a forum post itself says of its task: its name and its tags."""
    tags = {tag.name.casefold() for tag in getattr(thread, "applied_tags", [])}
    return {
        "title": (getattr(thread, "name", "") or "")[:120],
        "status": next(
            (name for name in STATUS_NAMES if name.casefold() in tags), STATUS_NAMES[0]
        ),
        "priority": next(
            (name for name in PRIORITY_NAMES if name.casefold() in tags),
            PRIORITY_NAMES[0],
        ),
    }


def discord_task_data(message: Any, channel_override: Any = None) -> dict[str, Any]:
    content = message.content.strip()
    channel = channel_override or getattr(message, "channel", None)
    assignee_match = re.search(r"^\s*Assigned to\s*:\s*(.+?)\s*$", content, re.MULTILINE | re.IGNORECASE)
    description_match = re.search(
        r"^\s*Description\s*:\s*(.*)$", content, re.MULTILINE | re.IGNORECASE | re.DOTALL
    )
    description = description_match.group(1).strip() if description_match else content
    assignees = parse_assignees(assignee_match.group(1) if assignee_match else "")
    task = thread_task(channel)
    title = task["title"] or content.splitlines()[0][:120]
    return {
        "title": title[:120],
        "status": task["status"],
        "priority": task["priority"],
        "assignees": assignees,
        "assignee": ", ".join(assignees),
        "description": description,
        "size": "",
        "start_date": "",
        "target_date": "",
        "closed": task["status"] == DONE_STATUS,
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


def field_value(item: dict[str, Any], field_name: str, key: str) -> str:
    """The value of one field of an item, empty when unset.

    A field of the Project comes first, then an issue field of the
    organisation, whose date reads from `value`.
    """
    project_value = next(
        (
            field.get(key) or ""
            for field in (item.get("fieldValues") or {}).get("nodes", [])
            if (field.get("field") or {}).get("name") == field_name and field.get(key)
        ),
        "",
    )
    if project_value:
        return project_value
    issue_values = ((item.get("content") or {}).get("issueFieldValues") or {}).get("nodes") or []
    return next(
        (
            (node.get("value") if key == "date" else node.get(key)) or ""
            for node in issue_values
            if node and (node.get("field") or {}).get("name") == field_name
        ),
        "",
    )


def github_item_data(
    item: dict[str, Any],
    assignee_field_name: str = "Owner",
    priority_field_name: str = "Task Priority",
    size_field_name: str = "Size",
    start_date_field_name: str = "Start date",
    target_date_field_name: str = "Target date",
) -> dict[str, Any]:
    content = item.get("content") or {}
    status = field_value(item, "Status", "name") or STATUS_NAMES[0]
    labels = (content.get("labels") or {}).get("nodes", [])
    priority_labels = {name.casefold() for name in PRIORITY_NAMES}
    priority = next(
        (
            label["name"]
            for label in labels
            if label.get("name", "").casefold() in priority_labels
        ),
        field_value(item, priority_field_name, "name") or PRIORITY_NAMES[0],
    )
    logins = [
        login
        for node in (content.get("assignees") or {}).get("nodes", [])
        if (login := node.get("login"))
    ]
    if not logins:
        logins = parse_assignees(
            field_value(item, assignee_field_name, "text")
            or field_value(item, "Assigned to", "text")
        )
    return {
        "title": content.get("title", "Nouvelle tache"),
        "url": content.get("url", ""),
        "status": status,
        "priority": priority,
        "assignees": logins,
        "assignee": ", ".join(logins),
        "description": issue_description(content.get("body") or ""),
        "size": field_value(item, size_field_name, "name"),
        "start_date": field_value(item, start_date_field_name, "date"),
        "target_date": field_value(item, target_date_field_name, "date"),
        "closed": content.get("state") in {"CLOSED", "MERGED"},
    }


def task_state(task: dict[str, Any]) -> tuple[str, ...]:
    """The fields a notification announces, to compare two versions of a task."""
    return (
        task["title"],
        task["status"],
        task["priority"],
        task["assignee"],
        task.get("size", ""),
        task.get("start_date", ""),
        task.get("target_date", ""),
    )


def card_state(task: dict[str, Any]) -> tuple[str, ...]:
    """Everything the post of a task shows, to know when to rewrite it."""
    return (
        *task_state(task),
        task.get("description", "").strip(),
        str(bool(task.get("closed"))),
    )


def same_card(first: dict[str, Any], second: dict[str, Any]) -> bool:
    """Two versions of a task that read the same, whatever the login case."""
    return tuple(value.casefold() for value in card_state(first)) == tuple(
        value.casefold() for value in card_state(second)
    )


def discord_comment_body(message: Any) -> str:
    """A reply posted in a Discord post, as the body of a GitHub comment."""
    lines = [
        GITHUB_MARKER,
        f"**{message.author.display_name}** on [Discord]({message.jump_url}):",
        "",
    ]
    if content := message.clean_content.strip():
        lines.append(content)
    lines.extend(
        f"[{attachment.filename}]({attachment.url})"
        for attachment in message.attachments
    )
    return "\n".join(lines)


def comment_of_message(comments: list[dict[str, Any]], message_url: str) -> dict | None:
    """The GitHub comment the bot wrote from a Discord message."""
    return next(
        (
            comment
            for comment in comments
            if GITHUB_MARKER in (comment.get("body") or "")
            and f"]({message_url})" in (comment.get("body") or "")
        ),
        None,
    )


def github_item_comments(item: dict[str, Any]) -> list[dict[str, str]]:
    """The latest comments of an issue or pull request, oldest first.

    A draft item has no conversation, so no comment.
    """
    nodes = ((item.get("content") or {}).get("comments") or {}).get("nodes") or []
    return [
        {
            "id": node["id"],
            "url": node.get("url") or "",
            "body": node.get("body") or "",
            "author": (node.get("author") or {}).get("login") or "ghost",
        }
        for node in nodes
        if node and node.get("id")
    ]


def github_item_has_all_comments(item: dict[str, Any]) -> bool:
    """Whether the listing carries every comment, so a missing one was deleted."""
    comments = (item.get("content") or {}).get("comments") or {}
    nodes = comments.get("nodes") or []
    return comments.get("totalCount", len(nodes)) <= len(nodes)


def is_bot_comment(comment: dict[str, str]) -> bool:
    """A comment the bot wrote from a Discord reply."""
    return GITHUB_MARKER in comment["body"]


def github_comment_message(comment: dict[str, str]) -> str:
    """A GitHub comment as a Discord message, cut to the Discord size limit."""
    header = f"**{comment['author']}** on GitHub:"
    # The angle brackets keep Discord from unfurling a preview of the issue.
    footer = f"<{comment['url']}>"
    body = comment["body"].strip()
    room = DISCORD_MESSAGE_LIMIT - len(header) - len(footer) - 2
    if len(body) > room:
        body = body[: room - 1].rstrip() + "\N{HORIZONTAL ELLIPSIS}"
    return f"{header}\n{body}\n{footer}"


def is_bot_message(message: Any) -> bool:
    return bool(message.author.bot)
