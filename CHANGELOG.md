# Changelog

Every notable change to Horizon Bot. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [2.0.0] — 2026-10-19

Version 2 makes the synchronisation complete — conversations, assignees, sizes, dates and closing now travel both ways — and adds a task form that lives in a Discord channel.

### Added

- **Task form.** One card in the channel named by `DISCORD_FORM_CHANNEL_ID`, shared by the team. Leave its post list empty for a new task, or pick a post to load its task. It edits the title and the description in a one-input dialog, the status, the priority, the size and the assignees with select menus, and the start and target dates on a calendar drawn in the card. Saving an edit writes only the fields that changed, without undoing a change made on GitHub in the meantime. An archived post is reached by pasting its link.
- **Conversations both ways.** A reply inside a post becomes a comment on its issue, and a comment on the issue becomes a reply in the post. Editing or deleting a message is mirrored too, and every message is announced with a *New message* card.
- **Closing.** The `Done` tag closes the issue and archives the post; leaving `Done` reopens it. An issue closed on GitHub moves to `Done` and archives its post; reopened, it goes back to `In progress`.
- **Size and dates.** A single-select size and the start and target dates are synchronised, shown on the cards and announced. Each may be a field of the Project or an issue field of the organisation.
- **Task card in the posts.** A post the bot opens starts with the task, one field a line — status, priority, size, assignees, dates, description and the GitHub link — rewritten whenever the task changes.
- **Development environment.** `.\run.ps1 -Dev` and `./run.sh --dev` read `.env.dev` instead of `.env`, through the new `HORIZON_ENV_FILE` variable, and `.vscode/launch.json` starts the bot or the tests under the debugger.
- New settings: `DISCORD_FORM_CHANNEL_ID`, `GITHUB_SIZE_FIELD_NAME`, `GITHUB_START_DATE_FIELD_NAME` and `GITHUB_TARGET_DATE_FIELD_NAME`, all optional.
- The bot logs its version at startup (`bot_horizon.__version__`).
- The form, the task cards and the new notification wording come in the ten languages of `!lang`; the form card is redrawn when the language changes.
- Test suites for the GitHub client and the form: 157 tests in all.
- MIT license.

### Changed

- **Editing the opening message of a post** now updates the description and the assignees of its issue.
- **Assignees** are synchronised both ways after the creation, not only at it.
- **Posts the bot opened** can be renamed and re-tagged from Discord; they used to be read-only there.
- **More than 100 tasks.** The Project is read 100 items a page, up to 5000 items. At its first start, version 2 publishes the tasks that never had a post — the ones past the 100 items version 1 read, and the ones that existed before the bot first ran — without a notification card each.
- The issue body the bot writes holds the description and the `Source:` link only. The `Status:`, `Priority:` and `Assigned to:` lines of earlier bodies are no longer written, and still read.
- The issue of a post is read from the issue itself, so a reply or a change right after a creation no longer waits minutes for the Project listing to catch up.
- Dates on the cards are Discord timestamps, shown in the date format of each reader.
- discord.py 2.6 or newer is required, for the components of the form card.

### Fixed

- A reply inside a post created a new issue instead of a comment.
- Creating a task failed when the auto-add workflow of the Project added the issue first.
- Creating a task failed on a repository without the priority issue field, such as one of a personal account; the priority label is kept and a warning logged instead.
- A date held in an issue field of the organisation could not be written, and was never read back.
- Two quick changes made on Discord could be undone by a Project listing that had only caught up with the first.
- `.env.dev` and the other `.env.*` files were not ignored by git.
- `run.sh` keeps LF line endings whatever the checkout, through `.gitattributes`.

### Upgrading from 1.0

1. **Dependencies.** `run.ps1` and `run.sh` reinstall them on their own, since `requirements.txt` changed. By hand: `pip install -r requirements.txt`.
2. **Discord permissions.** On top of the ones of 1.0, the bot needs *Send Messages in Threads*, to copy GitHub comments into posts. *Manage Threads*, already required, now also archives and reopens posts. The invite URL with every permission: `https://discord.com/oauth2/authorize?client_id=<CLIENT_ID>&scope=bot&permissions=292057861120`.
3. **Task form (optional).** Create a text channel, read-only for everyone but the bot, and set its id in `DISCORD_FORM_CHANNEL_ID`. The card is posted on the next start.
4. **Size and dates (optional).** Add a single-select `Size` and two date fields `Start date` and `Target date` to the Project, or as issue fields of the organisation. Other names go in `GITHUB_SIZE_FIELD_NAME`, `GITHUB_START_DATE_FIELD_NAME` and `GITHUB_TARGET_DATE_FIELD_NAME`. A field that exists nowhere is left out of the form and of the cards.
5. **Restart the bot.** The first poll takes GitHub as it is as its baseline, as in 1.0: nothing is announced again, and every task linked to a post stays linked through the `Source:` line of its issue. A task no post matches, by link or by name, gets one, unless the archived posts cannot be listed. Existing posts get their task card the next time their task changes.

## [1.0.0]

First release.

- A post in the Discord forum creates a GitHub issue in the Project, with its status, priority and assignees, and a task created on the Project is published as a post.
- Renaming or re-tagging a post updates the issue, and a change on the Project board renames and re-tags the post.
- Deleting a post deletes its issue, and deleting a task deletes its post.
- Every action is announced as a card in a notification channel, in one of ten languages chosen with `!lang`.
- Launch scripts for Windows, Linux and macOS, English and French documentation.

[2.0.0]: https://github.com/SunriseMyG/Horizon-org/compare/v1.0.0...v2.0.0
[1.0.0]: https://github.com/SunriseMyG/Horizon-org/releases/tag/v1.0.0
