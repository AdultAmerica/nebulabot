import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

# Bootstrap admins. Further admins can be granted at runtime with /addadmin and
# are stored in the database; the IDs here can never be revoked from chat, so
# the bot can always be recovered by editing .env.
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x]

WEBHOOK_URL = os.getenv("WEBHOOK_URL")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")  # random string; verified on every incoming update
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/webhook")

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", 8000))

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///bot.db")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# Fallback timezone for anything created before an admin picks their own.
DEFAULT_TIMEZONE = os.getenv("TIMEZONE", "UTC")

# Minimum seconds between two commands from the same admin. Telegram itself
# rate-limits us, but this stops a stuck finger from queueing 40 posts.
THROTTLE_SECONDS = float(os.getenv("THROTTLE_SECONDS", "0.4"))

# Telegram refuses albums larger than this.
ALBUM_LIMIT = 10

# How many times to retry a send that failed with a transient Telegram error.
SEND_RETRIES = int(os.getenv("SEND_RETRIES", "3"))

# Ceiling on how long we will honour a Telegram "retry after N seconds". Beyond
# it the run is abandoned rather than holding a worker for a quarter hour.
MAX_RETRY_AFTER = int(os.getenv("MAX_RETRY_AFTER", "300"))

# Missed runs collapse into one and are only delivered if under this age.
MISFIRE_GRACE = int(os.getenv("MISFIRE_GRACE", "3600"))

VERSION = "2.0.0"


def validate_timezone(name: str) -> str | None:
    """Return the canonical tz name if it resolves, else None."""
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return None
    return name


def tz_or_default(name: str | None) -> ZoneInfo:
    for candidate in (name, DEFAULT_TIMEZONE, "UTC"):
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            continue
    return ZoneInfo("UTC")
