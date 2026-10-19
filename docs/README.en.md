# Code documentation

How the bot is built, for anyone changing it. For what it does and how to run it, see the [root README](../README.md). Une version française existe : [README.fr.md](README.fr.md).

## Contents

1. [Module map](#module-map)
2. [Startup](#startup)
3. [The task, as the bot sees it](#the-task-as-the-bot-sees-it)
4. [Discord → GitHub](#discord--github)
5. [GitHub → Discord](#github--discord)
6. [The task form](#the-task-form)
7. [Linking a post to a task](#linking-a-post-to-a-task)
8. [Loop and echo prevention](#loop-and-echo-prevention)
9. [In-memory state](#in-memory-state)
10. [Notifications](#notifications)
11. [GitHub GraphQL operations](#github-graphql-operations)
12. [Error handling](#error-handling)
13. [Tests](#tests)
14. [Limitations and leads](#limitations-and-leads)

---

## Module map

| Module | Role | Imports Discord? |
| --- | --- | --- |
| [`config.py`](../bot_horizon/config.py) | `Settings`, a frozen dataclass built from the environment | no |
| [`sync.py`](../bot_horizon/sync.py) | Pure parsing and formatting: read a post, read a Project item, issue bodies, dates | no |
| [`notifications.py`](../bot_horizon/notifications.py) | The content of a notification card and of a post task card, in any language | no |
| [`languages.json`](../bot_horizon/languages.json) | The wording of the cards and of the form, one object per language | no |
| [`github_client.py`](../bot_horizon/github_client.py) | GraphQL client, one method per operation | no |
| [`form.py`](../bot_horizon/form.py) | The task form: the shared card, its calendar, its dialogs | yes |
| [`main.py`](../bot_horizon/main.py) | `HorizonBot`: event handlers, polling loop, in-memory state, form actions | yes |

The split matters: everything outside `main.py` and `form.py` is testable without a Discord connection, and `sync.py` and `notifications.py` have no I/O at all. Keep new parsing or formatting logic there rather than inside a handler. `form.py` only gathers input: every write goes through a `HorizonBot` method.

## Startup

`run()` in `main.py` loads the file named by `HORIZON_ENV_FILE` (`.env` by default, `.env.dev` with `run.ps1 -Dev`), builds `Settings`, instantiates `GitHubClient` and `HorizonBot`, then connects.

- `setup_hook` registers the `FormView` of the form card, so its components answer again after a restart.
- `on_ready` starts the polling loop as a background task, once — a Discord reconnection fires `on_ready` again, and the `if self.poll_task is None` guard keeps a single loop running. It also redraws the form card found in the last 50 messages of the form channel, or posts it.

`Settings.from_environment()` fails fast: a missing `DISCORD_TOKEN`, `DISCORD_CHANNEL_ID`, `GITHUB_TOKEN` or `GITHUB_PROJECT_ID` raises before the bot connects. Every other variable has a default, so the bot runs with the four required ones.

## The task, as the bot sees it

Both sides are normalised into the same dictionary, by `discord_task_data()` for a post and `github_item_data()` for a Project item:

```python
{
    "title": "Fix login",
    "status": "Backlog",          # one of STATUS_NAMES
    "priority": "Low",            # one of PRIORITY_NAMES
    "assignees": ["alice"],       # GitHub logins
    "assignee": "alice",          # the same, joined for display
    "description": "…",           # the issue body without the marker and Source line
    "size": "M",                  # an option of the size field, or ""
    "start_date": "2026-10-20",   # ISO dates, or ""
    "target_date": "2026-10-31",
    "closed": False,              # the issue state
    "url": "https://github.com/…",
}
```

Two tuples are drawn from it:

- `task_state()` — title, status, priority, assignee, size and both dates: **what a notification announces**. `state_changes()` turns two of them into `(key, before, after)` triples, which a card renders in its own language.
- `card_state()` — the same plus the description and the closed state: **what a post shows**. A difference there rewrites the post, announced or not. `same_card()` compares two of them ignoring the case, since GitHub returns a login in its own case.

Reading a post: `thread_task()` takes the title from the post name and the status and priority from the **first matching forum tag**, falling back to `Backlog` and `Low`. `Assigned to:` and `Description:` are read from the opening message with case-insensitive regexes, and assignee lists accept `,`, `;`, `/` or spaces as separators, with an optional `@`.

Reading a Project item: the status comes from the `Status` single-select field; the priority from an issue label matching a priority name, falling back to the configured priority field; the assignees from the issue's real assignees, falling back to the configured text field or a field named `Assigned to`; the size and the dates from the fields named in `GITHUB_SIZE_FIELD_NAME`, `GITHUB_START_DATE_FIELD_NAME` and `GITHUB_TARGET_DATE_FIELD_NAME` — a Project field first, then an issue field of the organisation (`Issue.issueFieldValues`), which GitHub keeps apart and only `updateIssueFieldValue` writes.

The issue body the bot writes is `GITHUB_MARKER`, the description, and a `Source:` line (`issue_body()`). `issue_description()` reads the description back out of it, and also out of the bodies older versions wrote, which carried `Status:`, `Priority:` and `Assigned to:` lines before a `Description:` header. `with_description()` replaces the description of a body and keeps its marker and its link.

## Discord → GitHub

Every handler is guarded by `is_configured_channel()` or `is_post()`, which accept the forum channel and its posts (`parent_id`).

### `on_message` — creation

A message in the notification channel is handled first, as a command, and never becomes a task. In the forum, bot messages are ignored, and a reply inside a post becomes a comment (see below). What is left is the opening message of a post — the one whose id is the thread id — which goes through creation: empty content is ignored, then `create_issue()` runs, which:

1. creates a real issue in `GITHUB_REPOSITORY_ID` (`createIssue`);
2. adds a priority label, creating it with a colour if the repository does not have it;
3. assigns the known logins, skipping and logging the unknown ones;
4. adds the issue to the Project (`addProjectV2ItemById`) — when the auto-add workflow of the Project got there first, it takes the existing item instead;
5. sets the Project `Status`, the repository-level priority field, the size and the dates;
6. closes the issue when the post was opened with the `Done` tag.

The issue body starts with `GITHUB_MARKER`, which is how the poll later recognises a task that came from Discord and must not be published back. The issue url is recorded in `known_github_sources` at once, so that a reply posted before the next poll already finds its issue.

### `on_message` — replies become comments

`is_post_reply()` recognises a message posted inside a forum post after its opening one. `comment_on_github()` hands it to `comment_from_discord()`, which finds the linked item with `find_linked_item()` — by the tracked key or the `Source:` line, never by title, since a reply on the wrong issue is worse than a reply not copied — and adds a comment (`addComment`), then announces it with a `COMMENTED` card. A draft item has no conversation, so a reply on one is only logged.

The comment body is built by `discord_comment_body()`: `GITHUB_MARKER`, the author's display name with a link to the Discord message, the content with mentions resolved (`clean_content`), and one link per attachment.

### `on_raw_message_edit` and `on_raw_message_delete` — edited and deleted messages

The raw events fire even for a message the cache no longer holds, after a restart for instance. An edit whose content and attachments did not change — Discord reports one when it unfurls a link — is dropped.

- An edited **reply** rewrites its comment: `edit_comment_from_discord()` reads the comments of the issue and finds the one the bot wrote from that message by the link to the message it carries (`comment_of_message()`).
- A deleted **reply** deletes that comment the same way, through `delete_comment_from_discord()`.
- An edited **opening message** of a post someone opened by hand goes through `sync_opening_message()`: the description and the assignees it now reads are written to the issue (`update_linked_task()`), and an assignee change is announced.

Bot messages are skipped, and so is the deletion of a message the bot deleted itself (`messages_deleted_by_bot`).

### `on_thread_update` — modification

Fires when a post is renamed, tagged, archived, pinned or put in slow mode. The handler:

1. drops the echo of an edit the bot itself just made (see [Loop and echo prevention](#loop-and-echo-prevention));
2. diffs `thread_task(before)` against `thread_task(after)` — name and tags — and **returns if nothing tracked changed**, which keeps archiving or pinning from writing to GitHub;
3. calls `update_issue_from_discord()`, which updates the title, replaces the priority label, sets the Project status and the native priority field, and returns the item key;
4. when the `Done` tag arrives it closes the issue and archives the post, and when it leaves it reopens the issue;
5. records the task as it now stands in `github_writes_from_discord`, then notifies.

Posts the bot opened are handled like any other: the echo guard, not the author of the opening message, is what keeps the bot from answering its own edits.

### `on_thread_delete` — deletion

Ignores deletions the bot performed itself, then `delete_task_from_discord()` finds the linked item and deletes the issue, falling back to removing the item from the Project if the issue cannot be deleted. A draft item only ever has its Project item removed. The key is then forgotten locally so the next poll does not treat it as a GitHub-side deletion.

## GitHub → Discord

`poll_github()` runs forever, every `GITHUB_POLL_INTERVAL_SECONDS`, calling `poll_once()`:

```
list_items()  →  {key: item}          key = issue url, or item id for a draft
│                                       read 100 items a page, up to 50 pages
├─ first run (known_github_items empty)
│     rebuild missing links, record every task as the baseline, announce nothing
│
├─ key not seen before
│     record it
│     └─ no GITHUB_MARKER and no Source line → publish a forum post,
│        write the link back to GitHub, announce a creation
│
├─ key known, card_state changed                       apply_github_change()
│     ├─ it matches a write the bot made from Discord → swallow the echo
│     ├─ issue just closed → status Done; just reopened while Done → In progress
│     └─ otherwise → rewrite the post (name, tags, task card, archive state),
│        announce what task_state shows changed
│
├─ every key: comments                                 relay_github_comments()
│     new → post them; edited → rewrite their copy; deleted → delete their copy
│     (comments carrying GITHUB_MARKER came from Discord and are left alone)
│
└─ key missing from the listing
      delete the forum post, forget the key, announce a deletion
```

The first iteration deliberately announces nothing: it takes the current GitHub state as the baseline. Without that, every existing task would be republished to Discord at each restart.

### The post of a task

`refresh_post()` brings a post in line with its task. It renames and re-tags the post only when its signature differs, and for a post the bot opened (`owns_post()`, from the thread owner) it rewrites the opening message with `post_card()` when the card differs. A post someone opened by hand keeps its opening message: the bot cannot edit a message it did not write.

An archived post cannot be edited, so `refresh_thread()` unarchives it for the time of the edit and archives it again; a close or a reopen sets the archive state it ends with. `publish_to_discord()` opens a new post with the same card, and requires at least one forum tag matching the status or priority — with none it raises, which is logged, and the task is retried on the next poll.

### Closed ↔ Done

A closed issue is a `Done` task. `apply_github_change()` moves a task closed on GitHub to `Done` and archives its post; a task reopened while still `Done` goes back to `In progress` and its post comes back. On Discord, `on_thread_update` closes the issue when the `Done` tag arrives and reopens it when it leaves. Archiving a post by hand changes nothing on GitHub: Discord also archives inactive posts on its own.

### Comments

`list_items()` brings the last 100 comments of every issue, with their total count. `relay_github_comments()` compares them with `known_github_comments`, which holds the body and the url of each comment already seen:

- a new comment is posted in the linked post, formatted by `github_comment_message()` and cut to the 2000 characters of a Discord message, with the mentions disabled so that a GitHub comment cannot ping the server, and announced with a `COMMENTED` card;
- an edited comment rewrites its copy, and a deleted one deletes it. `relayed_message()` finds the copy from `relayed_comments` while the bot runs, and after a restart from the comment link the copy ends with. A missing comment counts as deleted only when the listing carries every comment of the issue (`github_item_has_all_comments()`).

The first run only records the comments, so a restart does not post a conversation again; a task first seen later has its existing comments copied into its new post.

## The task form

`form.py` holds the Discord components; `HorizonBot` holds what they do.

```
FormView (LayoutView, persistent, custom ids horizon:card:*): one Container
├─ heading: New task, or Edit a task with the link of the post
├─ ChannelSelect of the forum posts: empty → new task, a post → load its task
├─ Title, Description: a Section each, with the Edit button opening its dialog
├─ status, priority, size, assignees: a heading and a select menu each
├─ start and target dates: a Section each, with the Choose button
├─ footer: what happened, who used the card last
└─ Create/Save · Cancel · Paste a link

calendar (the same card, while a date is picked)
├─ the date and its month
├─ Previous month · Next month · Clear · Back
└─ two select menus of days, 1-16 and 17-end, each labelled with its weekday
```

The card is one message the whole team shares, which is why it is a single `FormView` held by the bot (`HorizonBot.form_view`), registered in `setup_hook` so that its fixed custom ids answer again after a restart. A click or a dialog answers by editing the card (`show()`); a save or a post loaded after a deferral edits the message directly (`redraw()`), which a bot can do on its own message at any time.

It is a `LayoutView`, Discord's components v2: one card holds text, select menus and buttons together, up to 40 components and 4000 characters, which the card stays under however long the description. The month and weekday names come from `languages.json` (`months`, `weekdays`).

Discord has no text input inside a message. The Edit buttons and Paste a link open a `FieldModal`, a dialog of one input filled with the current value; sending it calls `FormView.write()`, which fills the card, or loads the post of a link. Create without a title opens the title dialog straight away. A link that leads to no post says why in the footer.

- `form_options()` gives the card its choices — the statuses and priorities, the options of the size field, the assignable logins of the repository, which date fields the Project has. They are read when the card is set up and kept 10 minutes. A field the Project lacks is left out of the card.
- `create_task_from_form()` opens the post with its tags and card, creates the issue with every field and the `Source:` line, then adds the issue link to the card. If the issue cannot be created, the post is deleted again.
- `load_task_for_form()` reads the task of a post from GitHub, refusing a post outside the forum (`TaskError("not_a_task")`) or one with no issue (`TaskError("task_not_found")`).
- Saving reads the task from GitHub again and applies to it only `changed_fields()` — what the user changed from the values the card loaded — so that a field left alone cannot undo a change made on GitHub since. `save_task_from_form()` then writes those fields (`update_task()`), closes or reopens the issue when `Done` comes or goes, refreshes the post, records the write as an echo to swallow, and announces the changes. The card goes back to an empty new task, with the result in its footer.

`ensure_form_panel()` redraws the card of the previous run as an empty new task, or posts it when the channel lacks it, and deletes the panels of earlier versions, whose custom ids nothing answers any more.

`TaskError` carries the key of its message in `languages.json`, with the values to put in it, which the card shows in its footer. Any other failure is logged and shown as `failed`, the task kept in the card. A picked start date after the target date, or the reverse, is refused with `dates_order`.

## Linking a post to a task

The durable link is the `Source:` line in the GitHub task body, holding the Discord post URL. `discord_thread_id()` extracts the post id from it.

- A task created from Discord or from the form carries the line from the start, since it is part of the body.
- A task created on the Project gets it right after publication: `set_discord_source()` rewrites the issue body (`updateIssue`) or the draft body (`updateProjectV2DraftIssue`).

The Project item listing lags behind GitHub, by up to several minutes after an issue joins the Project. `find_linked_item()` therefore reads an issue url key straight from the issue (`item_of_issue()`, through `resource(url:)` and `Issue.projectItems`), which has no such delay, and only falls back to the listing for a draft or an issue outside the Project.

Because it lives in GitHub, the link survives a restart. Three fallbacks cover the cases where it does not exist yet:

1. **`reconcile_links()` at startup** — for every task with no `Source:` line, it looks through the forum posts (active *and* archived) for one whose name matches the task title exactly. The bot names posts after task titles and keeps both in sync, so an exact match identifies the pair. Two posts with the same name leave the task unlinked, with a log line, rather than guessing. A task no post matches at all was never published — it was past the 100 items version 1 read, or there before the bot first ran — and `catch_up()` publishes it, without a notification card. It only does so when the archived posts could be listed, since a missing archive would make published tasks look unpublished.
2. **`known_github_sources`** — the in-memory map, which covers the window between a creation and the poll that first sees it.
3. **The post name** — `find_linked_item()` accepts a title as a last resort, and only trusts it when exactly one task matches.

## Loop and echo prevention

Each side writes to the other, so every write can come back as an event. One guard per echo:

| Guard | Echo it kills |
| --- | --- |
| `is_bot_message()` | The bot's own messages, edits included |
| `threads_deleted_by_bot` | The `on_thread_delete` of a post the bot just deleted |
| `messages_deleted_by_bot` | The `on_raw_message_delete` of a copy the bot just deleted |
| `threads_edited_by_bot` | The `on_thread_update` of a post the bot just renamed or re-tagged |
| `github_writes_from_discord` | A task change the bot pushed from Discord, coming back through a later poll |
| `is_bot_comment()` | A comment the bot wrote from a Discord reply, seen again by the next poll |

The post guard records **what the bot is about to write**, and discards the incoming event only if it matches exactly:

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

The GitHub guard keeps a short list per task of the states the bot wrote (`expect_github()`, built on `latest_details()`). `is_own_write()` swallows a polled state matching any of them, and settles every older one with it: the Project listing lags, so a poll may show the first of two quick Discord changes after the second was made, and must not undo it.

Together with the "no tracked change, no write" rule in `on_thread_update`, one user action produces exactly one GitHub write and one notification.

## In-memory state

All of it lives on `HorizonBot` and is rebuilt from GitHub at startup.

| Attribute | Type | Holds |
| --- | --- | --- |
| `known_github_items` | `set[str]` | The keys seen on the last poll, to detect creations and deletions |
| `known_github_details` | `dict[str, dict]` | Every field of each task as last read, to detect changes |
| `known_github_sources` | `dict[str, str]` | Key → Discord post URL |
| `known_github_comments` | `dict[str, dict]` | Key → comment id → (body, url) of the comments already seen |
| `relayed_comments` | `dict[str, int]` | GitHub comment id → the Discord message it was copied to |
| `threads_deleted_by_bot` | `set[int]` | Post ids the bot is deleting |
| `messages_deleted_by_bot` | `set[int]` | Message ids the bot is deleting |
| `threads_edited_by_bot` | `dict[int, tuple]` | Post id → the signature the bot is writing |
| `github_writes_from_discord` | `dict[str, list]` | Key → the task states the bot wrote and has not seen back yet |
| `assignable_users` | `tuple` | When the assignable logins were read, and the logins |

`forget_github_item()` clears a key from the four `known_github_*` tables and from the pending echoes, and is the only correct way to drop a task.

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

A date is written as a Discord timestamp (`discord_date()`, `<t:…:D>`), so every reader sees it in their own date format. It is set at noon UTC, which keeps the same calendar day in every time zone a team works from.

`post_card()` writes the opening message of a post the bot owns, one field a line and no icon: status, priority, size, assignees, the dates when there are any, the description, and the GitHub link. It stays under the 2000 characters of a message by shortening the description.

### Languages

The command, its syntax and the ten codes on offer are documented in the [root README](../README.md#card-language). What follows is how it is wired.

Everything crossing this module is keyed by the internal names in `STATE_KEYS` — `title`, `status`, `priority`, `assignee`, `size`, `start_date`, `target_date` — never by a label a reader sees. `state_changes()` returns `(key, before, after)` triples and `task_details()` a dict keyed the same way; `build_notification()` and `post_card()` are where a key becomes a word, through `language_words(language)`. The form reads its labels from the same file, through `HorizonBot.words`.

Adding a language means adding one object to `languages.json` with the same keys as the others, which `test_notifications.py` enforces. An unknown or missing code falls back to English rather than raising, so a bad value in `.env` cannot stop a notification.

`HorizonBot.language` holds the code in use. It is read at startup from `.horizon-language`, falling back to `NOTIFICATION_LANGUAGE`, and written back by `store_language()` when the `!lang` command changes it. A file that cannot be written is logged: the language still applies for that run. The form card is redrawn in the new language when `!lang` changes it.

The command itself lives in `handle_language_command()`, reached from `on_message` for any message in the notification channel. It requires the Manage Server permission, refuses an unknown code, and confirms with a sample card built in the new language.

`HorizonBot.notify()` returns immediately when no notification channel is configured, sends the embed, and falls back to the text version on `discord.Forbidden` — the case where the bot lacks *Embed Links*. Any other failure is logged and swallowed: the synchronisation is already done by the time a card is sent, and must not fail because of it.

Adding a field to a card means touching `build_notification()` only. Adding a tracked field means touching `task_state()` and `STATE_KEYS`, which must stay in the same order, and adding its label to every language in `languages.json`.

## GitHub GraphQL operations

Everything goes through `GitHubClient._query()`, which posts to `https://api.github.com/graphql` and raises on a GraphQL `errors` payload. `list_items()` and `item_of_issue()` read the same `ItemFields` fragment, so an item has the same shape wherever it comes from.

| Method | Operations |
| --- | --- |
| `create_issue` | `createIssue`, then label, assignees, `addProjectV2ItemById`, status, priority, size, dates, `closeIssue` |
| `update_task` | Writes the given fields only: `updateIssue`, priority label and field, status, size, dates, assignees, `closeIssue` / `reopenIssue` |
| `update_issue_from_discord` / `update_linked_task` | `find_linked_item`, then `update_task` |
| `update_optional_fields` | The size and the dates: through `set_issue_field` when the repository has an issue field of that name, otherwise `updateProjectV2ItemFieldValue` or `clearProjectV2ItemFieldValue`; a field neither holds is skipped |
| `issue_fields` / `set_issue_field` | `Repository.issueFields`, read once; `updateIssueFieldValue` to write one, `deleteIssueFieldValue` to clear it |
| `update_native_priority` | `set_issue_field` on the priority issue field; skipped with a warning when the field is missing, as on a personal account |
| `set_assignees` | `removeAssigneesFromAssignable` and `addAssigneesToAssignable`, so the assignees become exactly the given logins |
| `comment_from_discord` | `find_linked_item`, then `addComment` |
| `edit_comment_from_discord` / `delete_comment_from_discord` | The issue comments, then `updateIssueComment` / `deleteIssueComment` on the one copied from the message |
| `item_of_issue` | `resource(url:)` with `Issue.projectItems`, the Project item without waiting for the listing |
| `add_issue_to_project` | `addProjectV2ItemById`, or the existing item when the auto-add workflow added it first |
| `add_label` | Reads `Repository.labels`, `createLabel` when missing, `addLabelsToLabelable` |
| `assignable_users` | `Repository.assignableUsers`, for the form |
| `project_metadata` / `project_field` | The Project fields, read once and read again when a field or an option is missing |
| `set_discord_source` | `updateIssue` or `updateProjectV2DraftIssue` |
| `delete_task_from_discord` | `deleteIssue`, falling back to `deleteProjectV2Item` |
| `list_items` | Every Project item, 100 a page, with content, field values and last 100 comments |

`create_project_item()` and `update_project_text()` are left over from the draft-item approach and are not called by anything. They still work, but the live path creates real issues.

## Error handling

The rule is that an action already performed must not be undone by a failure in what follows.

- Each event handler wraps its work in `try/except Exception` and logs with `logger.exception`, so one bad event never kills the client.
- The polling loop catches everything per iteration and keeps polling; a failed iteration is retried on the next one, since `known_github_items` is only updated after a successful pass.
- An assignee that cannot be set does not prevent the issue from being created, nor from joining the Project.
- A notification that cannot be sent is logged, never raised.
- A form action that fails says so in the footer of the card, which keeps what was filled in.

## Tests

| File | Covers | Needs |
| --- | --- | --- |
| `test_sync.py` | Parsing posts and Project items, issue bodies, dates, URL extraction | nothing |
| `test_notifications.py` | Card content, field diffing, task cards, dates, API limits, text fallback | nothing |
| `test_github_client.py` | Pagination, field writes, assignees, comment lookup, on a faked `_query` | httpx |
| `test_form.py` | The form card: its layout, the calendar, its dialogs, saving | discord.py |
| `test_bot.py` | Synchronisation both ways, echoes, closing, comments, the form, on fakes | discord.py |

```bash
.venv/bin/python -m unittest tests.test_sync tests.test_notifications tests.test_github_client tests.test_form tests.test_bot
```

`test_bot.py` drives the real `HorizonBot` against a `FakeGitHub` that keeps one task the way the listing returns it, and a `FakeThread` whose edits change it the way Discord applies them. `poll_once()` runs one poll iteration, and `KnownTaskTestCase` starts from a bot that has already taken its baseline. That is the pattern to reuse for any new behaviour in the event handlers.

## Limitations and leads

- **Labels (20), assignees (10) and comments (100) are not paginated** per issue. Past 100 comments, a deletion among the oldest ones is not seen.
- **5000 items at most**: `list_items()` stops after 50 pages and logs a warning.
- **Discord rate-limits post renames**, to about two per post every ten minutes. discord.py waits out the limit inside `refresh_post()`, which holds the poll loop meanwhile.
- **A message is edited and deleted where it was written.** A Discord-born comment edited on GitHub, or a GitHub-born copy edited on Discord, does not travel back.
- **Discord attachment links expire** after about a day, so the links copied into a GitHub comment stop working; the file itself is not uploaded.
- **The polling baseline** is taken at startup, so changes made while the bot was down are never noticed.
- **The post list of the card shows active posts only**, the threads Discord lists; an archived post is reached by pasting its link.
- **One card for the whole team**: two people using it at once take turns on the same task.
