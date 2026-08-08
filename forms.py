"""The single-value input flow.

Rather than one FSM state per editable field, every "tap a button, type a
value" interaction routes through one state and one registry. Adding a new
setting means adding a prompt and an apply rule, not a new handler.
"""

import json
import logging

import config
from access import set_pref, user_tz
from db import (
    KIND_CRON, KIND_DAILY, KIND_INTERVAL, KIND_ONCE, ContentGroup, Queue,
    SessionLocal, Target,
)
from scheduling import sync_group, sync_queue, validate_cron
from utils import (
    parse_chat_id, parse_datetime, parse_duration, parse_hhmm, parse_time_list,
)

log = logging.getLogger(__name__)

CLEAR_WORDS = {"off", "none", "clear", "-", "0m", "reset"}

# field -> (title, instructions). The instructions are shown verbatim, so this
# is where the bot explains itself.
PROMPTS: dict[str, tuple[str, str]] = {
    "name": ("🏷 Name", "Send a short name. It only shows in the panel, never in the post."),
    "caption": (
        "✏️ Caption",
        "Send the caption text. Formatting follows the group's parse mode "
        "(HTML by default): <code>&lt;b&gt;bold&lt;/b&gt;</code>, "
        "<code>&lt;i&gt;italic&lt;/i&gt;</code>, "
        "<code>&lt;a href=\"…\"&gt;link&lt;/a&gt;</code>.\n\n"
        "Send <code>off</code> to remove the caption.\n"
        "Limit: 1024 characters on a media post, 4096 on a text-only one.",
    ),
    "interval": (
        "⏱ Interval",
        "How long between posts. Examples: <code>45s</code>, <code>30m</code>, "
        "<code>2h</code>, <code>1d 6h</code>, <code>1w</code>.\n\n"
        "Anything under a minute will get you rate-limited by Telegram; a few "
        "hours is the usual choice for a channel.",
    ),
    "cron": (
        "🗓 Cron expression",
        "Five fields: <code>minute hour day month weekday</code>.\n\n"
        "<code>0 */4 * * *</code> — every 4 hours, on the hour\n"
        "<code>30 9 * * 1-5</code> — 09:30 on weekdays\n"
        "<code>0 12 1 * *</code> — noon on the 1st of each month\n\n"
        "Read in the group's timezone.",
    ),
    "daily": (
        "🌅 Daily times",
        "One or more clock times, comma separated: <code>09:00, 15:30, 21:00</code>.\n"
        "Read in the group's timezone.",
    ),
    "runat": (
        "📌 One-shot time",
        "When to fire, once: <code>2026-08-14 18:30</code>, or a relative "
        "<code>in 90m</code>. Read in your timezone.",
    ),
    "jitter": (
        "🎲 Jitter",
        "Random delay added to each run so posts never land on the exact same "
        "second — <code>90s</code>, <code>5m</code>. Send <code>off</code> to "
        "disable.",
    ),
    "tz": (
        "🌍 Timezone",
        "An IANA name: <code>UTC</code>, <code>America/New_York</code>, "
        "<code>Europe/Berlin</code>, <code>Asia/Tokyo</code>.",
    ),
    "quiet": (
        "🌙 Quiet hours",
        "A window in which scheduled runs are skipped: <code>23:00-07:00</code>. "
        "Wrapping past midnight is fine. Send <code>off</code> to clear.",
    ),
    "startat": (
        "🚦 Start of window",
        "The schedule stays dormant until this moment: <code>2026-09-01 08:00</code> "
        "or <code>in 2d</code>. Send <code>off</code> to clear.",
    ),
    "endat": (
        "🏁 End of window",
        "The schedule retires after this moment: <code>2026-12-31 23:59</code>. "
        "Send <code>off</code> to clear.",
    ),
    "maxposts": (
        "🔢 Maximum posts",
        "Stop automatically after this many successful posts. Send "
        "<code>off</code> for unlimited.",
    ),
    "delafter": (
        "🧹 Auto-delete",
        "Delete each post this long after it goes out — <code>6h</code>, "
        "<code>2d</code>. Handy for time-limited offers. Send <code>off</code> "
        "to keep posts forever.",
    ),
    "tags": (
        "🏷 Tags",
        "Comma-separated labels used by search: <code>promo, weekend, vip</code>.",
    ),
    "target": (
        "🎯 Add target",
        "Send the channel or chat ID (<code>-1001234567890</code>) or its "
        "@username.\n\n"
        "The bot must already be an admin there with permission to post. "
        "Forward any message from the channel to <code>/id</code>, or run "
        "<code>/id</code> inside the chat, to look the ID up.\n\n"
        "For a forum topic, append the thread id: <code>-1001234567890:42</code>.",
    ),
    "addbtn": (
        "🔘 Add button",
        "Send <code>Text | https://example.com</code>.\n\n"
        "The button is appended to the last row. Use <b>Bulk edit</b> to build "
        "several rows at once.",
    ),
    "bulkbtn": (
        "🧩 Bulk button editor",
        "One row per line, each button as <code>Text | URL</code>. Put two "
        "buttons on the same row by separating them with <code>;;</code>:\n\n"
        "<code>Join channel | https://t.me/x\n"
        "Website | https://a.com ;; Support | https://b.com</code>\n\n"
        "This replaces all existing buttons. Send <code>off</code> to clear.",
    ),
    "addadmin": (
        "🔑 Add admin",
        "Send the new admin's numeric Telegram user ID. They can find it by "
        "sending <code>/id</code> to this bot — though they must already be an "
        "admin to get a reply, so use @userinfobot or forward one of their "
        "messages here.",
    ),
    "broadcast": (
        "📣 Broadcast",
        "Send the message text. It goes once to every enabled target chat "
        "across all your groups. HTML formatting is allowed.",
    ),
    "find": ("🔎 Find groups", "Send a word to match against names, tags and captions."),
    "default_interval": (
        "⏱ Default interval",
        "Used for every new group you create: <code>2h</code>, <code>1d</code>.",
    ),
    "default_target": (
        "🎯 Default target",
        "New groups get this target automatically. Send a chat ID or @username, "
        "or <code>off</code> to stop pre-filling it.",
    ),
}


def prompt_for(field: str) -> str:
    title, body = PROMPTS.get(field, ("Value", "Send the new value."))
    return f"<b>{title}</b>\n\n{body}"


def _is_clear(text: str) -> bool:
    return text.strip().lower() in CLEAR_WORDS


def _parse_buttons_bulk(text: str) -> list[list[dict]] | None:
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        row = []
        for chunk in line.split(";;"):
            if "|" not in chunk:
                return None
            label, url = (part.strip() for part in chunk.split("|", 1))
            if not label or not url:
                return None
            row.append({"text": label, "url": url})
        if row:
            rows.append(row)
    return rows or None


# --------------------------------------------------------------------------
# Appliers
# --------------------------------------------------------------------------

def _apply_schedule_field(obj, field: str, text: str, tz) -> str | None:
    """Shared between groups and queues. Returns an error, or None on success."""
    if field == "interval":
        seconds = parse_duration(text)
        if not seconds:
            return "Could not read that duration. Try <code>30m</code> or <code>2h</code>."
        obj.interval_seconds = seconds
        obj.schedule_kind = KIND_INTERVAL
    elif field == "cron":
        error = validate_cron(text.strip())
        if error:
            return f"Invalid cron: <code>{error}</code>"
        obj.cron_expr = text.strip()
        obj.schedule_kind = KIND_CRON
    elif field == "daily":
        times = parse_time_list(text)
        if not times:
            return "Use 24-hour times like <code>09:00, 18:30</code>."
        obj.daily_times = ",".join(times)
        obj.schedule_kind = KIND_DAILY
    elif field == "runat":
        when = parse_datetime(text, tz)
        if not when:
            return "Use <code>2026-08-14 18:30</code> or <code>in 90m</code>."
        obj.run_at = when
        obj.schedule_kind = KIND_ONCE
    elif field == "jitter":
        obj.jitter_seconds = 0 if _is_clear(text) else (parse_duration(text) or 0)
    elif field == "tz":
        name = config.validate_timezone(text.strip())
        if not name:
            return "Unknown timezone. Use an IANA name like <code>Europe/Berlin</code>."
        obj.timezone_name = name
    elif field == "quiet":
        if _is_clear(text):
            obj.quiet_start = obj.quiet_end = None
        else:
            parts = text.replace("—", "-").replace("to", "-").split("-")
            if len(parts) != 2:
                return "Use <code>23:00-07:00</code>."
            start, end = parse_hhmm(parts[0]), parse_hhmm(parts[1])
            if start is None or end is None:
                return "Use <code>23:00-07:00</code>."
            obj.quiet_start, obj.quiet_end = start, end
    elif field == "startat":
        obj.start_at = None if _is_clear(text) else parse_datetime(text, tz)
        if obj.start_at is None and not _is_clear(text):
            return "Could not read that date."
    elif field == "endat":
        obj.end_at = None if _is_clear(text) else parse_datetime(text, tz)
        if obj.end_at is None and not _is_clear(text):
            return "Could not read that date."
    else:
        return f"Unknown field {field}."
    return None


SCHEDULE_FIELDS = {"interval", "cron", "daily", "runat", "jitter", "tz", "quiet", "startat", "endat"}


def apply_group(group_id: int, field: str, text: str, user_id: int) -> tuple[bool, str]:
    tz = user_tz(user_id)
    db = SessionLocal()
    try:
        group = db.get(ContentGroup, group_id)
        if not group:
            return False, "That group no longer exists."

        if field in SCHEDULE_FIELDS:
            error = _apply_schedule_field(group, field, text, tz)
            if error:
                return False, error
            feedback = "Schedule updated."
        elif field == "name":
            group.name = text.strip()[:64]
            feedback = "Renamed."
        elif field == "caption":
            group.caption = None if _is_clear(text) else text
            feedback = "Caption cleared." if group.caption is None else "Caption saved."
        elif field == "maxposts":
            if _is_clear(text):
                group.max_posts = None
            elif text.strip().isdigit() and int(text) > 0:
                group.max_posts = int(text)
            else:
                return False, "Send a positive whole number, or <code>off</code>."
            feedback = "Limit updated."
        elif field == "delafter":
            group.delete_after = None if _is_clear(text) else parse_duration(text)
            if group.delete_after is None and not _is_clear(text):
                return False, "Could not read that duration."
            feedback = "Auto-delete updated."
        elif field == "tags":
            group.tags = ",".join(t.strip() for t in text.split(",") if t.strip())[:200]
            feedback = "Tags updated."
        elif field == "target":
            raw = text.strip()
            thread = None
            if ":" in raw and not raw.startswith("@"):
                raw, _, suffix = raw.rpartition(":")
                thread = int(suffix) if suffix.isdigit() else None
            chat_id = parse_chat_id(raw)
            if not chat_id:
                return False, "That does not look like a chat ID or @username."
            exists = db.query(Target).filter(
                Target.group_id == group_id, Target.chat_id == chat_id
            ).first()
            if exists:
                return False, "That target is already on the list."
            db.add(Target(group_id=group_id, chat_id=chat_id, thread_id=thread, enabled=True))
            feedback = f"Target {chat_id} added."
        elif field == "addbtn":
            if "|" not in text:
                return False, "Use <code>Text | https://example.com</code>."
            label, url = (part.strip() for part in text.split("|", 1))
            if not label or not url:
                return False, "Both a label and a URL are needed."
            rows = group.buttons()
            if rows and len(rows[-1]) < 3:
                rows[-1].append({"text": label, "url": url})
            else:
                rows.append([{"text": label, "url": url}])
            group.buttons_json = json.dumps(rows)
            feedback = "Button added."
        elif field == "bulkbtn":
            if _is_clear(text):
                group.buttons_json = None
                feedback = "Buttons cleared."
            else:
                rows = _parse_buttons_bulk(text)
                if not rows:
                    return False, "Every button needs <code>Text | URL</code>."
                group.buttons_json = json.dumps(rows)
                feedback = f"Saved {sum(len(r) for r in rows)} button(s)."
        else:
            return False, f"Unknown field {field}."

        db.commit()
    finally:
        db.close()

    if field in SCHEDULE_FIELDS:
        sync_group(group_id)
    return True, feedback


def apply_queue(queue_id: int, field: str, text: str, user_id: int) -> tuple[bool, str]:
    tz = user_tz(user_id)
    db = SessionLocal()
    try:
        queue = db.get(Queue, queue_id)
        if not queue:
            return False, "That queue no longer exists."
        if field in SCHEDULE_FIELDS:
            error = _apply_schedule_field(queue, field, text, tz)
            if error:
                return False, error
            feedback = "Schedule updated."
        elif field == "name":
            queue.name = text.strip()[:64]
            feedback = "Renamed."
        else:
            return False, f"Unknown field {field}."
        db.commit()
    finally:
        db.close()

    if field in SCHEDULE_FIELDS:
        sync_queue(queue_id)
    return True, feedback


def apply_settings(user_id: int, field: str, text: str) -> tuple[bool, str]:
    if field == "tz":
        name = config.validate_timezone(text.strip())
        if not name:
            return False, "Unknown timezone. Try <code>Europe/Berlin</code>."
        set_pref(user_id, "timezone_name", name)
        return True, f"Timezone set to {name}."
    if field == "default_interval":
        seconds = parse_duration(text)
        if not seconds:
            return False, "Could not read that duration."
        set_pref(user_id, "default_interval", seconds)
        return True, "Default interval saved."
    if field == "default_target":
        if _is_clear(text):
            set_pref(user_id, "default_target", None)
            return True, "Default target cleared."
        chat_id = parse_chat_id(text)
        if not chat_id:
            return False, "That does not look like a chat ID or @username."
        set_pref(user_id, "default_target", chat_id)
        return True, f"Default target set to {chat_id}."
    return False, f"Unknown setting {field}."
