"""Parsing and formatting helpers shared by the handlers and the scheduler."""

import html
import re
from datetime import datetime, timedelta, timezone

_DURATION_RE = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>[a-z]*)", re.I)
_UNITS = {
    "": 1, "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "week": 604800, "weeks": 604800,
}


def parse_duration(value: str) -> int | None:
    """"90", "30m", "2h30m", "1d 6h" -> seconds. None if unparseable."""
    if not value:
        return None
    total = 0
    matched = False
    for match in _DURATION_RE.finditer(value.strip().lower()):
        unit = match.group("unit")
        if unit not in _UNITS:
            return None
        total += float(match.group("value")) * _UNITS[unit]
        matched = True
    if not matched or total <= 0:
        return None
    return int(total)


def format_duration(seconds: int | None) -> str:
    if not seconds:
        return "—"
    parts = []
    for label, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if seconds >= size:
            count, seconds = divmod(seconds, size)
            parts.append(f"{count}{label}")
    return " ".join(parts[:3])


def parse_time_list(value: str) -> list[str] | None:
    """"9:00, 18:30" -> ["09:00", "18:30"]. None if any entry is invalid."""
    times = []
    for chunk in re.split(r"[,\s]+", value.strip()):
        if not chunk:
            continue
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", chunk)
        if not match:
            return None
        hour, minute = int(match.group(1)), int(match.group(2))
        if not (0 <= hour < 24 and 0 <= minute < 60):
            return None
        times.append(f"{hour:02d}:{minute:02d}")
    return times or None


def parse_hhmm(value: str) -> int | None:
    """"22:30" -> minutes past midnight."""
    times = parse_time_list(value)
    if not times or len(times) != 1:
        return None
    hour, minute = times[0].split(":")
    return int(hour) * 60 + int(minute)


def format_hhmm(minutes: int | None) -> str:
    if minutes is None:
        return "—"
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def parse_datetime(value: str, tz) -> datetime | None:
    """"2026-08-09 14:30" in `tz` -> naive UTC. Date alone means midnight."""
    value = value.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            local = datetime.strptime(value, fmt)
        except ValueError:
            continue
        return local.replace(tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)
    # Relative form: "in 2h", "2h"
    seconds = parse_duration(value[3:] if value.lower().startswith("in ") else value)
    if seconds:
        return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=seconds)
    return None


def to_local(dt: datetime | None, tz) -> datetime | None:
    """Naive-UTC column value -> aware local time."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone(tz)


def format_dt(dt: datetime | None, tz, with_relative: bool = True) -> str:
    local = to_local(dt, tz)
    if local is None:
        return "—"
    out = local.strftime("%Y-%m-%d %H:%M %Z")
    if with_relative:
        delta = int((local - datetime.now(tz)).total_seconds())
        if delta > 0:
            out += f" (in {format_duration(delta)})"
        elif delta < 0:
            out += f" ({format_duration(-delta)} ago)"
    return out


def esc(value) -> str:
    return html.escape(str(value), quote=False)


def chunked(seq, size):
    for start in range(0, len(seq), size):
        yield seq[start:start + size]


def parse_chat_id(value: str) -> str | None:
    """Accept -1001234567890, @channel, or a t.me link."""
    value = (value or "").strip()
    if not value:
        return None
    match = re.fullmatch(r"(?:https?://)?t\.me/(?:s/)?([A-Za-z][\w]{3,})", value)
    if match:
        return "@" + match.group(1)
    if re.fullmatch(r"-?\d{5,}", value):
        return value
    if re.fullmatch(r"@?[A-Za-z][\w]{3,}", value):
        return value if value.startswith("@") else "@" + value
    return None


def rich_text(message) -> str:
    """The text of a message as its author meant it to be formatted.

    Formatting applied in the Telegram client arrives as entities, which
    `html_text` renders into tags — that is what we want. But a user who typed
    the tags by hand sends no entities, and `html_text` would escape them into
    literal `&lt;b&gt;`, so that case has to keep the raw text instead.
    """
    if getattr(message, "entities", None):
        return message.html_text
    return message.text or ""


def truncate(value: str, limit: int = 60) -> str:
    value = " ".join((value or "").split())
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"
