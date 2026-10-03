# Code documentation

How the bot is built, for anyone changing it. For what it does and how to run it, see the [root README](../README.md). Une version française existe : [README.fr.md](README.fr.md).

## Contents

1. [Module map](#module-map)
2. [Startup](#startup)
3. [The task, as the bot sees it](#the-task-as-the-bot-sees-it)
4. [Discord → GitHub](#discord--github)
5. [GitHub → Discord](#github--discord)
6. [Linking a post to a task](#linking-a-post-to-a-task)
7. [Loop and echo prevention](#loop-and-echo-prevention)
8. [In-memory state](#in-memory-state)
9. [Notifications](#notifications)
10. [GitHub GraphQL operations](#github-graphql-operations)
11. [Error handling](#error-handling)
12. [Tests](#tests)
13. [Limitations and leads](#limitations-and-leads)

---

## Module map

| Module | Role | Imports Discord? |
| --- | --- | --- |
| [`config.py`](../bot_horizon/config.py) | `Settings`, a frozen dataclass built from the environment | no |
| [`sync.py`](../bot_horizon/sync.py) | Pure parsing and formatting: read a post, read a Project item, extract an id | no |
| [`notifications.py`](../bot_horizon/notifications.py) | Builds the content of a notification card, in any language | no |
| [`languages.json`](../bot_horizon/languages.json) | The card wording, one object per language | no |
| [`github_client.py`](../bot_horizon/github_client.py) | GraphQL client, one method per operation | no |
| [`main.py`](../bot_horizon/main.py) | `HorizonBot`: event handlers, polling loop, in-memory state | yes |

The split matters: everything outside `main.py` is testable without a Discord connection, and `sync.py` and `notifications.py` have no I/O at all. Keep new parsing or formatting logic there rather than inside a handler.

## Startup

`run()` in `main.py` loads `.env`, builds `Settings`, instantiates `GitHubClient` and `HorizonBot`, then connects. `on_ready` starts the polling loop as a background task, once — a Discord reconnection fires `on_ready` again, and the `if self.poll_task is None` guard keeps a single loop running.

`Settings.from_environment()` fails fast: a missing `DISCORD_TOKEN`, `DISCORD_CHANNEL_ID`, `GITHUB_TOKEN` or `GITHUB_PROJECT_ID` raises before the bot connects. Every other variable has a default, so the bot runs with the four required ones.

## The task, as the bot sees it

Both sides are normalised into the same dictionary, by `discord_task_data()` for a post and `github_item_data()` for a Project item:

```python
{
    "title": "Fix login",
    "status": "Backlog",        # one of STATUS_NAMES
    "priority": "Low",          # one of PRIORITY_NAMES
    "assignees": ["alice"],     # GitHub logins
    "assignee": "alice",        # the same, joined for display
    "description": "…",
}
```

`task_state()` reduces it to the four tracked fields — title, status, priority, assignee — as a tuple. Comparing two tuples is how the bot decides whether anything happened; `state_changes()` turns a difference into `(key, before, after)` triples, which a card renders in its own language. Anything not in that tuple is invisible to change detection.

Reading a post: the status and priority come from the **first matching forum tag**, falling back to `Backlog` and `Low`. The title is the post name. `Assigned to:` and `Description:` are read from the starter message with case-insensitive regexes, and assignee lists accept `,`, `;`, `/` or spaces as separators, with an optional `@`.

Reading a Project item: the status comes from the `Status` single-select field; the priority from an issue label matching a priority name, falling back to the configured priority field; the assignees from the issue's real assignees, falling back to the configured text field or a field named `Assigned to`.

## Discord → GitHub

Three handlers, all guarded by `is_configured_channel()`, which accepts the forum channel itself and any of its posts (`parent_id`).

### `on_message` — creation

A message in the notification channel is handled first, as a command, and never becomes a task. Every other message in the forum goes through creation: bot messages and empty content are ignored, then `create_issue()` runs, which:

1. creates a real issue in `GITHUB_REPOSITORY_ID` (`createIssue`);
2. adds a priority label, creating it with a colour if the repository does not have it;
3. assigns the known logins, skipping and logging the unknown ones;
4. adds the issue to the Project (`addProjectV2ItemById`);
5. sets the Project `Status` and the repository-level priority field.

The issue body is built by `discord_message_body()` and starts with `GITHUB_MARKER`, which is how the poll later recognises a task that came from Discord and must not be published back.

### `on_thread_update` — modification

Fires when a post is renamed, tagged, archived, pinned or put in slow mode. The handler:

1. drops the echo of an edit the bot itself just made (see [Loop and echo prevention](#loop-and-echo-prevention));
2. ignores posts whose starter message belongs to the bot;
3. diffs `task_state(before)` against `task_state(after)` and **returns if nothing tracked changed** — this is what keeps archiving or pinning from writing to GitHub;
4. calls `update_issue_from_discord()`, which updates the title, replaces the priority label, sets the Project status and the native priority field, and returns the item key;
5. records that write in `github_writes_from_discord`, then notifies.

Note that assignees are not written back here: only the title, status and priority travel in this direction after creation.

### `on_thread_delete` — deletion

Ignores deletions the bot performed itself, then `delete_task_from_discord()` finds the linked item and deletes the issue, falling back to removing the item from the Project if the issue cannot be deleted. A draft item only ever has its Project item removed. The key is then forgotten locally so the next poll does not treat it as a GitHub-side deletion.

## GitHub → Discord

`poll_github()` runs forever, every `GITHUB_POLL_INTERVAL_SECONDS`. One iteration:

```
list_items()  →  {key: item}          key = issue url, or item id for a draft
│
├─ first run (known_github_items empty)
│     rebuild missing links, record every state as the baseline, announce nothing
│
├─ key not seen before
│     record its state
│     └─ no GITHUB_MARKER and no Source line → publish a forum post,
│        write the link back to GitHub, announce a creation
│
├─ key known, state changed
│     ├─ it matches what we just wrote from Discord → swallow the echo
│     └─ otherwise → edit the post, announce a modification
│
└─ key missing from the listing
      delete the forum post, forget the key, announce a deletion
```

The first iteration deliberately announces nothing: it takes the current GitHub state as the baseline. Without that, every existing task would be republished to Discord at each restart.

`publish_to_discord()` requires at least one forum tag matching the status or priority; with none it raises, which is logged, and the task is retried on the next poll.

## Linking a post to a task

The durable link is the `Source:` line in the GitHub task body, holding the Discord post URL. `discord_thread_id()` extracts the post id from it.

- A task created from Discord carries the line from the start, since it is part of the body.
- A task created on the Project gets it right after publication: `set_discord_source()` rewrites the issue body (`updateIssue`) or the draft body (`updateProjectV2DraftIssue`).

Because it lives in GitHub, the link survives a restart. Three fallbacks cover the cases where it does not exist yet:

1. **`reconcile_links()` at startup** — for every task with no `Source:` line, it looks through the forum posts (active *and* archived) for one whose name matches the task title exactly. The bot names posts after task titles and keeps both in sync, so an exact match identifies the pair. Two posts with the same name leave the task unlinked, with a log line, rather than guessing.
2. **`known_github_sources`** — the in-memory map, which covers the window between connecting and the end of the first poll.
3. **The post name** — `find_linked_item()` accepts a title as a last resort, and only trusts it when exactly one task matches.

## Loop and echo prevention

Each side writes to the other, so every write can come back as an event. Four guards, each for a specific echo:

| Guard | Echo it kills |
| --- | --- |
| `is_bot_message()` | The bot's own messages and the posts it opened |
| `threads_deleted_by_bot` | The `on_thread_delete` of a post the bot just deleted |
| `threads_edited_by_bot` | The `on_thread_update` of a post the bot just renamed or re-tagged |
| `github_writes_from_discord` | The Project change the bot just pushed, coming back through the next poll |

The last two work the same way: before writing, the bot records **what it is about to write**, and discards the incoming event only if it matches exactly.

```python
# before editing a post
signature = (name, frozenset(tag.name for tag in tags))
if thread_signature(thread) == signature:
    return                                    # already correct, do not edit at all
self.threads_edited_by_bot[thread.id] = signature
```

```python
# on receiving a post update
if self.threads_edited_by_bot.pop(after.id, None) == thread_signature(after):
    return                                    # this is our own edit
```

Comparing a signature rather than a bare id matters: if a user changes something else in the meantime, the event no longer matches and is processed normally. An edit that fails removes its own entry so it cannot swallow a later user change, and a post already in the target state is never edited, which removes the event at the source.

Together with the "no tracked change, no write" rule in `on_thread_update`, one user action produces exactly one GitHub write and one notification.

## In-memory state

All of it lives on `HorizonBot` and is rebuilt from GitHub at startup.

| Attribute | Type | Holds |
| --- | --- | --- |
| `known_github_items` | `set[str]` | The keys seen on the last poll, to detect creations and deletions |
| `known_github_states` | `dict[str, tuple]` | The last known state of each task, to detect changes |
| `known_github_sources` | `dict[str, str]` | Key → Discord post URL |
| `threads_deleted_by_bot` | `set[int]` | Post ids the bot is deleting |
| `threads_edited_by_bot` | `dict[int, tuple]` | Post id → the signature the bot is writing |
| `github_writes_from_discord` | `dict[str, tuple]` | Key → the (title, status, priority) the bot just pushed |

`forget_github_item()` clears a key from the first three and from the pending echo, and is the only correct way to drop a task.

## Notifications

`notifications.py` describes a card without importing Discord:

```python
Notification(
    author="Task created • Discord → GitHub",
    title="🟢 Fix login",
    color=0x57F287,
    url="https://github.com/org/repo/issues/42",   # makes the title clickable
    fields=[("📈 Status", "Ready", True), …],      # (name, value, inline)
)
```

`build_notification()` assembles it, dropping empty details and links — the Discord API rejects a field with an empty value — and truncating to the API limits (256 for a title, 1024 for a field value). `notification_embed()` in `main.py` turns it into a `discord.Embed`, and `notification_text()` renders the same card as plain text.

### Languages

The command, its syntax and the ten codes on offer are documented in the [root README](../README.md#card-language). What follows is how it is wired.

Everything crossing this module is keyed by the internal names in `STATE_KEYS` — `title`, `status`, `priority`, `assignee` — never by a label a reader sees. `state_changes()` returns `(key, before, after)` triples and `task_details()` a dict keyed the same way; `build_notification()` is the single place where a key becomes a word, through `language_words(language)`.

Adding a language means adding one object to `languages.json` with the same keys as the others, which `test_notifications.py` enforces. An unknown or missing code falls back to English rather than raising, so a bad value in `.env` cannot stop a notification.

`HorizonBot.language` holds the code in use. It is read at startup from `.horizon-language`, falling back to `NOTIFICATION_LANGUAGE`, and written back by `store_language()` when the `!lang` command changes it. A file that cannot be written is logged: the language still applies for that run.

The command itself lives in `handle_language_command()`, reached from `on_message` for any message in the notification channel. It requires the Manage Server permission, refuses an unknown code, and confirms with a sample card built in the new language.

`HorizonBot.notify()` returns immediately when no notification channel is configured, sends the embed, and falls back to the text version on `discord.Forbidden` — the case where the bot lacks *Embed Links*. Any other failure is logged and swallowed: the synchronisation is already done by the time a card is sent, and must not fail because of it.

Adding a field to a card means touching `build_notification()` only. Adding a tracked field means touching `task_state()` and `STATE_KEYS`, which must stay in the same order, and adding its label to every language in `languages.json`.

## GitHub GraphQL operations

Everything goes through `GitHubClient._query()`, which posts to `https://api.github.com/graphql` and raises on a GraphQL `errors` payload.

| Method | Operations |
| --- | --- |
| `create_issue` | `createIssue`, then label, assignees, `addProjectV2ItemById`, status, priority |
| `update_issue_from_discord` | `updateIssue`, `removeLabelsFromLabelable`, `addLabelsToLabelable`, status, priority |
| `update_native_priority` | Reads `Repository.issueFields`, writes `updateIssueFieldValue` |
| `add_label` | Reads `Repository.labels`, `createLabel` when missing, `addLabelsToLabelable` |
| `assign_issue` | One `user(login:)` query per login, then `addAssigneesToAssignable` |
| `update_project_select` / `update_project_status` | `updateProjectV2ItemFieldValue` with the option id |
| `set_discord_source` | `updateIssue` or `updateProjectV2DraftIssue` |
| `delete_task_from_discord` | `deleteIssue`, falling back to `deleteProjectV2Item` |
| `list_items` | Reads the first 100 Project items with their content and field values |

`create_project_item()` and `update_project_text()` are left over from the draft-item approach and are not called by anything. They still work, but the live path creates real issues.

## Error handling

The rule is that an action already performed must not be undone by a failure in what follows.

- Each event handler wraps its work in `try/except Exception` and logs with `logger.exception`, so one bad event never kills the client.
- The polling loop catches everything per iteration and keeps polling; a failed iteration is retried on the next one, since `known_github_items` is only updated after a successful pass.
- An assignee that cannot be set does not prevent the issue from being created, nor from joining the Project.
- A notification that cannot be sent is logged, never raised.

## Tests

| File | Covers | Needs discord.py |
| --- | --- | --- |
| `test_sync.py` | Parsing posts and Project items, assignee lists, URL extraction | no |
| `test_notifications.py` | Card content, field diffing, API limits, text fallback | no |
| `test_bot.py` | Echo prevention, end to end, on a fake GitHub client | yes |

```bash
.venv/bin/python -m unittest tests.test_sync tests.test_notifications tests.test_bot
```

`test_bot.py` drives the real `HorizonBot` against a `FakeGitHub`, patching `notify` to record calls and forcing one poll iteration with `is_closed` returning `False` then `True`. That is the pattern to reuse for any new behaviour in the event handlers.

## Limitations and leads

- **`list_items` is not paginated** (`items(first: 100)`), and neither are labels (20) or assignees (10). A Project above 100 items needs cursor pagination.
- **No `on_message_edit` handler**, so editing a post body — including `Assigned to:` — never reaches GitHub.
- **`on_message` fires on every message in the forum**, including replies inside an existing post, each of which is treated as a new task. Reacting only to a post's starter message (`message.id == message.channel.id`) would restrict creation to new posts.
- **Assignees are not written back** by `update_issue_from_discord`, which only sends title, status and priority.
- **The polling baseline** is taken at startup, so changes made while the bot was down are never noticed.
