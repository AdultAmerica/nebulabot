# nebulabot

Telegram bot for composing media groups and posting them to your channels on a
schedule, driven by an inline admin panel. Every screen is also a command, and
every command is also a button.

---

## What it does

**Groups** are the unit of work: one post, plus the rules for delivering it —
media, caption, buttons, target chats, schedule and delivery options. A group
can repeat forever, run inside a date window, or fire once and retire.

**Queues** rotate a list of groups. Where a group reposts the same album on a
timer, a queue walks its rota one group per tick — a drip campaign rather than
a repeating ad.

---

## Quick start

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. Add the bot to your channel as an **administrator** with *Post messages*.
3. Fill in `.env` (see below) and start the bot.
4. DM it `/start`, set your timezone in ⚙️ **Settings**.
5. ➕ **New group** → send photos/videos → **Caption** → **Targets** →
   **Schedule** → ▶️ **Activate**.

👁 **Preview** posts the group to your own chat first, so you can see exactly
what subscribers will get before anything goes out.

Not sure of a channel's ID? Run `/id` inside the channel, or forward one of its
messages to the bot.

---

## Configuration

Copy `.env.example` to `.env` and fill it in.

| Variable | Required | Notes |
| --- | --- | --- |
| `BOT_TOKEN` | yes | From [@BotFather](https://t.me/BotFather). |
| `ADMIN_IDS` | yes | Comma-separated user IDs. Cannot be revoked from chat — always keep one you control. |
| `WEBHOOK_URL` | no | Set only for webhook mode. Leave unset to use polling. |
| `WEBHOOK_SECRET` | no | Webhook mode only. Any long random string (`openssl rand -hex 32`). |
| `WEBHOOK_PATH` | no | Defaults to `/webhook`. |
| `PORT` | no | Webhook mode only. Defaults to 8000; hosts usually inject this. |
| `DATABASE_URL` | no | Any SQLAlchemy URL. Defaults to `sqlite:///bot.db`. |
| `TIMEZONE` | no | Fallback IANA zone for admins who have not set their own. |
| `LOG_LEVEL` | no | Defaults to `INFO`. |
| `THROTTLE_SECONDS` | no | Minimum spacing between one admin's actions. Default `0.4`. |
| `SEND_RETRIES` / `MAX_RETRY_AFTER` | no | Retry behaviour for failed sends and flood waits. |
| `MISFIRE_GRACE` | no | How stale a missed run may be and still be delivered. Default `3600`. |

Admins added at runtime with `/addadmin` are stored in the database. Each admin
sees only the groups and queues they own.

---

## Features

### Composing

- **All media types** — photos, videos, GIFs, documents, audio, voice notes,
  video notes and stickers.
- **Automatic album batching** — Telegram caps an album at 10, so larger groups
  are split into consecutive albums. Photos and videos share an album; audio
  and documents album among themselves; the rest send individually.
- **Duplicate detection** — re-sending a file already in the group is skipped.
- **Reordering** — move items up and down, reverse the whole run, or turn on
  🔀 shuffle to reorder on every single post.
- **Captions** with HTML, MarkdownV2, Markdown or no parsing. Formatting typed
  by hand and formatting applied in the Telegram client both work.
- **URL buttons** in rows, added one at a time or laid out in bulk:
  ```
  Join | https://t.me/example
  Site | https://a.com ;; Help | https://b.com
  ```
- **Text-only posts** — a group with a caption and no media posts as a message.

### Targets

- Any number of destination chats per group, each individually enabled.
- Numeric IDs, `@usernames`, or `t.me` links; forum topics via `-100…:42`.
- 🔁 **Rotate targets** sends to one chat per run in turn instead of all at once.

### Scheduling

| Mode | Example | Behaviour |
| --- | --- | --- |
| Interval | `30m`, `2h`, `1d 6h` | Fixed spacing, forever |
| Cron | `0 */4 * * *` | Full five-field cron |
| Daily | `09:00, 18:30` | One or more clock times |
| Once | `2026-08-14 18:30` | Fires once, then retires |

Plus **jitter** (random spread so posts never look machine-timed), **quiet
hours** (skip runs inside a window, wrapping past midnight), a **start/end
window**, and a **maximum post count** that retires the group automatically.
All clock times are read in the group's timezone.

### Delivery options

Silent delivery · content protection (no forwarding or saving) · auto-pin ·
auto-delete after a delay · failure alerts by DM.

### Tools

`/stats` usage overview · `/history` recent deliveries with the exact Telegram
error · `/jobs` every registered job and its next fire time · `/health` runtime
diagnostics · `/export` and `/import` groups as JSON · `/backup` the whole
database · `/broadcast` to every target · `/pauseall` and `/resumeall` ·
`/admins` to manage access.

---

## Commands

**Panel** — `/start` `/menu` `/help [topic]` `/cancel`

**Groups** — `/new [name]` `/groups` `/group <id>` `/find <text>`
`/caption <id> <text>` `/target <id> <chat>` `/tag <id> <tags>`
`/interval <id> <30m>` `/cron <id> <expr>` `/daily <id> <09:00>`
`/preview <id>` `/postnow <id>` `/schedule <id>` `/pause <id>` `/resume <id>`
`/clone <id>` `/delete <id>` `/purge <id>`

**Queues** — `/queues` `/newqueue [name]` `/queue <id>` `/qadd <qid> <gid>`
`/qdel <qid> <gid>` `/qnext <qid>` `/qstart <id>` `/qstop <id>`

**Tools** — `/settings` `/timezone <zone>` `/stats` `/history` `/jobs`
`/health` `/export [id]` `/import` `/backup` `/broadcast <text>` `/admins`
`/addadmin <id>` `/deladmin <id>` `/pauseall` `/resumeall` `/id` `/ping`
`/version` `/whoami`

`/help` covers all of this in the bot itself, split into topics.

---

## Run modes

The bot picks its mode from `WEBHOOK_URL`:

- **Unset → polling.** Needs nothing but a bot token — no public URL, no TLS,
  no open port. This is the simplest way to run on a VPS.
- **Set → webhook.** Serves `POST /webhook` over aiohttp, plus a `GET /health`
  endpoint for uptime checks. Requires a public HTTPS endpoint, since Telegram
  will not deliver to plain HTTP.

---

## Deploying to a VPS (systemd)

Assumes Debian/Ubuntu. Runs in polling mode, so leave `WEBHOOK_URL` unset.

```sh
sudo useradd --system --home /opt/nebulabot nebulabot
sudo git clone https://github.com/AdultAmerica/nebulabot /opt/nebulabot
cd /opt/nebulabot

sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt

sudo cp .env.example .env
sudo nano .env                 # fill in BOT_TOKEN and ADMIN_IDS
sudo chmod 600 .env
sudo chown -R nebulabot:nebulabot /opt/nebulabot

sudo cp deploy/nebulabot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nebulabot
```

Check on it:

```sh
systemctl status nebulabot     # is it running
journalctl -u nebulabot -f     # live logs
sudo systemctl restart nebulabot
```

The unit restarts the bot on crash and starts it on boot.

## Deploying to a container host

A `Dockerfile` is included and takes precedence over buildpack detection. Set
the environment variables through the host's own configuration rather than
committing a `.env` file.

Note that `bot.db` is a SQLite file in the working directory. On hosts with
ephemeral filesystems it is erased on every redeploy, taking all groups, media,
schedules and history with it — mount a persistent volume, or point
`DATABASE_URL` at Postgres, before relying on it. `/backup` sends you a
snapshot of the file at any time.

---

## How scheduling survives restarts

Jobs live in the same database as the content, so schedules outlast the
process. On startup every active group and queue is re-registered from its row,
which is the source of truth — a job store written by an older version is
rebuilt rather than trusted.

Runs missed while the process was down collapse into one, dated at the most
recent time the job was due, and are delivered only if that time is under an
hour old (`MISFIRE_GRACE`). A brief restart therefore still posts, while a long
outage waits for the next slot instead of flushing a backlog into your channel.

---

## Upgrading from 1.x

Just deploy and restart. The schema migrates itself: missing columns and tables
are added on startup, and the old single `target_chat_id` on each group becomes
a row in the new `targets` table. Existing groups, media and schedules are kept.

Old-format jobs in the job store are discarded and re-registered from the group
rows, so a group left `queued` keeps running with no manual step.

---

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
BOT_TOKEN=... ADMIN_IDS=... .venv/bin/python bot.py
```

Run the tests — plain assert scripts, no pytest, each in its own temp
directory:

```sh
.venv/bin/python tests/run_all.py
```

They cover the delivery engine against a stub Bot (album batching, caption and
keyboard placement, target fan-out and rotation, pinning, auto-delete, post
limits, queue rotation, error capture), the schema migration from a v1
database, every form field and rendered screen, and a full pass through the
real dispatcher with synthetic updates.

### Layout

| File | Responsibility |
| --- | --- |
| `bot.py` | Entry point: dispatcher, middlewares, run modes |
| `config.py` | Environment and constants |
| `db.py` | Schema, sessions, forward-only migrations |
| `scheduling.py` | Triggers, delivery, retries, bookkeeping |
| `views.py` | Screen rendering — text plus keyboard |
| `keyboards.py` | Every inline keyboard |
| `forms.py` | Prompts and value appliers |
| `access.py` | Admin checks and user preferences |
| `middlewares.py` | Auth, throttling, error capture |
| `handlers/` | Command and callback routing |
