# Horizon Bot

A Discord bot that keeps a Discord forum channel and a GitHub Project in sync, in both directions, in real time.

Teams discuss work on Discord and track it on GitHub. Keeping the two aligned usually means writing every task twice and letting one copy rot. This bot removes that step: a forum post **is** a task, and a task **is** a forum post. Create, edit or delete either one and the other follows, with every change announced in a dedicated log channel.

Built for the [Horizon](https://github.com/play-horizon) team.

---

## What it does

| You do this… | …and the bot does that |
| --- | --- |
| Open a post in the Discord forum | Creates the GitHub issue, adds it to the Project, sets status, priority and assignees |
| Rename a post or change its tags | Updates the issue title, its priority label and its Project status |
| Delete a post | Deletes the linked issue, or removes it from the Project if the issue cannot be deleted |
| Create a task in the GitHub Project | Publishes it as a forum post, with the matching status and priority tags |
| Move a task on the Project board | Renames the post and swaps its tags to match |
| Delete a task from the Project | Deletes the matching forum post |

Every one of those actions posts a card in a notification channel, so the team sees the activity without watching both platforms.

### Notifications

Each action is announced as a Discord embed, colour-coded by action — green for a creation, blurple for a change, red for a deletion:

```
┃ Task created • Discord → GitHub
┃ 🟢 Set up the notification channel
┃ 📈 Status       🔸 Priority      👤 Assigned to
┃ Ready           High             alice, bob
┃ 🔗 Links
┃ GitHub • Discord
┃ Horizon                                    today at 20:46
```

A change lists exactly which fields moved:

```
┃ Task updated • GitHub → Discord
┃ ✏️ Set up the notification channel
┃ 📝 Changes
┃ • Status: Backlog → In progress
┃ • Assigned to: alice → alice, bob
```

One user action produces exactly one card: the bot recognises the echoes of its own writes and discards them, so a tag change does not bounce back and forth between the two platforms.

### Card language

The cards speak one of the ten most widely spoken languages in the world. Anyone who can manage the server sets it from the notification channel:

```text
!lang          list the languages, marking the one in use
!lang fr       switch the cards to French
```

| Code | Language | Code | Language |
| --- | --- | --- | --- |
| `en` | English | `ar` | العربية |
| `zh` | 中文 | `bn` | বাংলা |
| `hi` | हिन्दी | `pt` | Português |
| `es` | Español | `ru` | Русский |
| `fr` | Français | `ur` | اردو |

The command is typed in the notification channel, so the role meant to run it needs **Send Messages** there even though the channel is read-only for everyone else.

The bot answers with a sample card in the language just chosen, and remembers the choice across restarts. `NOTIFICATION_LANGUAGE` in `.env` sets the starting language; after that the command wins.

Only the wording of the cards is translated. Task titles, statuses, priorities and logins come from Discord and GitHub, so they stay as they are written there.

---

## How it works

```
       DISCORD                                        GITHUB
┌──────────────────────┐                      ┌──────────────────────┐
│   Forum channel      │   gateway events     │   GitHub Project     │
│   one post = one     │ ───────────────────▶ │   + repository       │
│   task               │                      │   issues             │
│                      │ ◀─────────────────── │                      │
└──────────────────────┘   polling, every     └──────────────────────┘
           │               30s by default                │
           └────────────────────┬─────────────────────────┘
                                ▼
                    ┌──────────────────────┐
                    │ Notification channel │
                    │  one card per action │
                    └──────────────────────┘
```

**Discord → GitHub is event-driven.** The bot reacts to gateway events (`on_message`, `on_thread_update`, `on_thread_delete`) and writes to the GitHub GraphQL API immediately.

**GitHub → Discord is poll-driven**, because GitHub Projects do not emit webhooks for every field change. The bot lists the Project items on an interval, compares each one against the state it remembers, and acts on the differences.

The link between a post and a task is a `Source:` line stored in the GitHub task body, holding the Discord post URL. It lives in GitHub, so it survives a restart, and missing links are rebuilt at startup by matching task titles against post names.

---

## Tech stack

| Component | Choice | Why |
| --- | --- | --- |
| Language | Python 3.10+ | PEP 604 unions, dataclasses, `asyncio` throughout |
| Discord | [discord.py](https://github.com/Rapptz/discord.py) 2.4+ | Gateway events, forum channels, embeds |
| GitHub | GraphQL API via [httpx](https://www.python-httpx.org/) | Projects v2 is GraphQL-only, with no REST equivalent |
| Config | [python-dotenv](https://github.com/theskumar/python-dotenv) | `.env` file, no secret in the repository |
| Tests | `unittest` from the standard library | Nothing extra to install to run them |

No database: the bot keeps a small in-memory state and stores the durable link inside GitHub itself.

---

## Getting started

### 1. Prerequisites

- Python 3.10 or newer
- A Discord server where you can create a **forum** channel and invite a bot
- A GitHub organisation Project (Projects v2) and a token that can write to it

### 2. Discord setup

1. Create an application and a bot in the [Discord Developer Portal](https://discord.com/developers/applications).
2. Enable **Message Content Intent**, under *Bot → Privileged Gateway Intents*.
3. Invite the bot with `View Channel`, `Send Messages`, `Read Message History`, `Create Posts`, `Embed Links` and `Manage Threads` — the last one is what allows it to delete forum posts.
4. In the forum channel, create tags named **exactly**:
   - status: `Backlog`, `Ready`, `In progress`, `In review`, `Done`
   - priority: `Low`, `Medium`, `High`, `Urgent`
5. Create a text channel for the notifications. Keep it read-only for everyone but the bot, except for the role allowed to run `!lang` — see [Card language](#card-language).

### 3. GitHub setup

Create a token with read/write access to the Project and the repository, then give the Project these fields:

- a single-select `Status` with the five status options above;
- a single-select `Task Priority` with the four priority options above;
- a text field `Owner` — `Assignee` is a name reserved by GitHub.

The field names are configurable, see the table below.

### 4. Configure

```bash
cp .env.example .env
```

| Variable | Required | Default | What it is |
| --- | --- | --- | --- |
| `DISCORD_TOKEN` | yes | — | Bot token from the Developer Portal |
| `DISCORD_CHANNEL_ID` | yes | — | The forum channel holding the tasks |
| `GITHUB_TOKEN` | yes | — | Token with write access to the Project |
| `GITHUB_PROJECT_ID` | yes | — | Global Project id, of the form `PVT_…` |
| `DISCORD_NOTIFICATION_CHANNEL_ID` | no | *disabled* | Channel receiving the action cards |
| `NOTIFICATION_LANGUAGE` | no | `en` | Starting language of the cards, until `!lang` changes it |
| `GITHUB_REPOSITORY_ID` | no | `""` | Repository that receives the created issues |
| `GITHUB_ORG_NAME` | no | `play-horizon` | Organisation owning the Project |
| `GITHUB_PROJECT_NUMBER` | no | `5` | Project number inside the organisation |
| `GITHUB_PRIORITY_FIELD_NAME` | no | `Priority` | Name of the priority field |
| `GITHUB_ASSIGNEE_FIELD_NAME` | no | `Assignees` | Name of the assignee text field |
| `GITHUB_POLL_INTERVAL_SECONDS` | no | `30` | Delay between two Project polls |

Leaving `DISCORD_NOTIFICATION_CHANNEL_ID` empty disables notifications; synchronisation is unaffected. `.env.example` also carries `CLIENT_ID`, the Discord application id used to build the bot invite URL; the bot itself never reads it.

### 5. Run

The launch scripts create the virtualenv, install the dependencies, check `.env` and start the bot:

```powershell
.\run.ps1      # Windows
```

```bash
./run.sh       # Linux, macOS, Git Bash
```

Pass `-NoInstall` / `--no-install` to skip the dependency step. By hand:

```bash
python -m venv .venv
source .venv/bin/activate       # .venv\Scripts\Activate.ps1 on Windows
pip install -r requirements.txt
python -m bot_horizon.main
```

---

## Writing a task

The post name becomes the task title. The body may use this format:

```text
Assigned to: alice, bob
Description:
What needs to be done.
```

Those are **GitHub logins**, not Discord handles. Separate several of them with a comma, a semicolon, a slash or a space; a leading `@` is accepted and duplicates are dropped. A login GitHub does not know is skipped with a warning in the logs — the other assignees still apply and the task is still created.

The first status tag and the first priority tag of the post fill the Project fields. A post with no status tag defaults to `Backlog`, and one with no priority tag to `Low`.

---

## Project structure

```
bot_horizon/
├── main.py            Discord client, event handlers, polling loop
├── github_client.py   GitHub GraphQL client: issues, Project items, labels
├── sync.py            Pure parsing and formatting, no I/O
├── notifications.py   Notification cards, free of any Discord import
├── languages.json     Card wording, in ten languages
└── config.py          Settings read from the environment
tests/
├── test_sync.py           Parsing and formatting
├── test_notifications.py  Card content and field diffing
└── test_bot.py            Echo prevention, end to end on fakes
docs/
├── README.en.md       Code documentation (English)
└── README.fr.md       Documentation du code (French)
```

## Tests

```bash
# No dependency needed
python -m unittest tests.test_sync tests.test_notifications

# From the virtualenv, including the end-to-end tests
.venv/bin/python -m unittest tests.test_sync tests.test_notifications tests.test_bot
.venv\Scripts\python -m unittest tests.test_sync tests.test_notifications tests.test_bot   # Windows
```

`tests/test_bot.py` needs discord.py, so run it from the virtualenv; the other two run on a bare Python.

## Deployment

The bot must run on a machine that stays up: its gateway connection only lives as long as the process. Use systemd, Docker, or any process supervisor. There is nothing to persist — the state is rebuilt from GitHub at startup.

---

## Known limitations

- **100 tasks per poll.** The Project query is not paginated, so a Project holding more than 100 items is only partially synchronised.
- **Changes made while the bot is down are lost.** Discord does not replay missed events, and the first poll takes the current GitHub state as its baseline rather than comparing it to anything.
- **Editing a post body does nothing.** Only the post name and its tags are synchronised, so changing `Assigned to:` after creation never reaches GitHub.
- **Posts published by the bot are read-only on Discord.** Their starter message belongs to the bot, which is how loops are avoided, so those tasks have to be edited on GitHub.

## Documentation

- [Code documentation (English)](docs/README.en.md) — architecture, data flow, loop prevention, GraphQL operations
- [Documentation du code (français)](docs/README.fr.md)

## License

This repository does not ship a license file yet, so default copyright applies: public to read, not to reuse.
