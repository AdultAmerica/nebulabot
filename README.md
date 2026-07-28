# nebulabot

Telegram bot for composing media groups and posting them to a target chat on a
schedule, driven by an inline admin panel.

## Configuration

Copy `.env.example` to `.env` and fill it in:

| Variable | Required | Notes |
| --- | --- | --- |
| `BOT_TOKEN` | yes | From [@BotFather](https://t.me/BotFather). |
| `ADMIN_IDS` | yes | Comma-separated Telegram user IDs allowed to use the panel. |
| `WEBHOOK_URL` | no | Set only for webhook mode. Leave unset to use polling. |
| `WEBHOOK_SECRET` | no | Webhook mode only. Any long random string (`openssl rand -hex 32`). |
| `PORT` | no | Webhook mode only. Defaults to 8000; hosts usually inject this. |

## Run modes

The bot picks its mode from `WEBHOOK_URL`:

- **Unset → polling.** Needs nothing but a bot token — no public URL, no TLS,
  no open port. This is the simplest way to run on a VPS.
- **Set → webhook.** Serves `POST /webhook` over aiohttp. Requires a public
  HTTPS endpoint, since Telegram will not deliver to plain HTTP.

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

A `Dockerfile` is included and takes precedence over buildpack detection.
Set the environment variables through the host's own configuration rather
than committing a `.env` file.

Note that `bot.db` is a SQLite file in the working directory. On hosts with
ephemeral filesystems it is erased on every redeploy, taking all groups,
media, and schedules with it — mount a persistent volume, or move to
Postgres, before relying on it.

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
BOT_TOKEN=... ADMIN_IDS=... .venv/bin/python bot.py
```
