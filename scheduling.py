"""The posting engine: triggers, delivery, retries, and bookkeeping.

Everything that actually talks to Telegram on a timer lives here. Handlers only
change rows and then call `sync_group` / `sync_queue`; this module is the only
place that decides *when* something fires and *what* gets sent.
"""

import asyncio
import json
import logging
import random
from datetime import datetime, timezone
from typing import NamedTuple

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup, InputMediaAudio,
    InputMediaDocument, InputMediaPhoto, InputMediaVideo,
)
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

import config
from db import (
    ALBUM_FAMILIES, DONE, DRAFT, KIND_CRON, KIND_DAILY, KIND_INTERVAL,
    KIND_ONCE, POSTED, QUEUED, ContentGroup, MediaItem, PostLog,
    Queue, QueueItem, SessionLocal, Target, engine, utcnow,
)
from utils import esc, parse_time_list

log = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(
    timezone="UTC",
    # Jobs live in the same database as the content, so schedules survive
    # restarts instead of dying with the process.
    jobstores={"default": SQLAlchemyJobStore(engine=engine)},
    job_defaults={
        # Runs missed while the process was down collapse into one, dated at
        # the most recent time the job was due, and delivered only if that time
        # is recent. A brief restart still posts; a long outage waits for the
        # next slot instead of dumping a backlog into the channel.
        "coalesce": True,
        "misfire_grace_time": config.MISFIRE_GRACE,
        "max_instances": 1,
    },
)

_bot: Bot | None = None

GROUP_PREFIX = "grp:"
QUEUE_PREFIX = "que:"
DELETE_PREFIX = "del:"

PARSE_MODES = {
    "HTML": ParseMode.HTML,
    "MarkdownV2": ParseMode.MARKDOWN_V2,
    "Markdown": ParseMode.MARKDOWN,
    "none": None,
}

_INPUT_MEDIA = {
    "photo": InputMediaPhoto,
    "video": InputMediaVideo,
    "audio": InputMediaAudio,
    "document": InputMediaDocument,
}


def attach_bot(bot: Bot):
    global _bot
    _bot = bot


# --------------------------------------------------------------------------
# Trigger construction
# --------------------------------------------------------------------------

def _aware(dt: datetime | None):
    return dt.replace(tzinfo=timezone.utc) if dt else None


def build_triggers(obj) -> list:
    """Turn a group's or queue's schedule columns into APScheduler triggers.

    Returns an empty list when the schedule is not yet complete enough to run,
    which is how a half-configured group stays harmlessly idle.
    """
    tz = config.tz_or_default(obj.timezone_name)
    common = {
        "timezone": tz,
        "start_date": _aware(obj.start_at),
        "end_date": _aware(obj.end_at),
        "jitter": obj.jitter_seconds or None,
    }
    kind = obj.schedule_kind or KIND_INTERVAL

    if kind == KIND_INTERVAL:
        if not obj.interval_seconds:
            return []
        return [IntervalTrigger(seconds=obj.interval_seconds, **common)]

    if kind == KIND_CRON:
        if not obj.cron_expr:
            return []
        try:
            return [_cron_from_expr(obj.cron_expr, **common)]
        except ValueError as exc:
            log.warning("Bad cron %r: %s", obj.cron_expr, exc)
            return []

    if kind == KIND_DAILY:
        times = parse_time_list(obj.daily_times or "")
        if not times:
            return []
        triggers = []
        for entry in times:
            hour, minute = entry.split(":")
            triggers.append(CronTrigger(hour=int(hour), minute=int(minute), **common))
        return triggers

    if kind == KIND_ONCE:
        if not obj.run_at:
            return []
        return [DateTrigger(run_date=_aware(obj.run_at), timezone=tz)]

    return []


def _cron_from_expr(expr: str, **kwargs) -> CronTrigger:
    """`CronTrigger.from_crontab` cannot take jitter or a window, so parse here."""
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError("expected 5 fields: minute hour day month day_of_week")
    minute, hour, day, month, day_of_week = fields
    return CronTrigger(
        minute=minute, hour=hour, day=day, month=month, day_of_week=day_of_week,
        **kwargs,
    )


def validate_cron(expr: str) -> str | None:
    """Return an error message, or None when the expression is usable."""
    try:
        _cron_from_expr(expr, timezone=config.tz_or_default(None))
    except (ValueError, TypeError) as exc:
        return str(exc)
    return None


def describe_schedule(obj) -> str:
    kind = obj.schedule_kind or KIND_INTERVAL
    if kind == KIND_INTERVAL:
        from utils import format_duration
        return f"every {format_duration(obj.interval_seconds)}" if obj.interval_seconds else "interval (unset)"
    if kind == KIND_CRON:
        return f"cron `{obj.cron_expr}`" if obj.cron_expr else "cron (unset)"
    if kind == KIND_DAILY:
        return f"daily at {obj.daily_times}" if obj.daily_times else "daily (unset)"
    if kind == KIND_ONCE:
        return "once" if obj.run_at else "once (unset)"
    return kind


# --------------------------------------------------------------------------
# Job registration
# --------------------------------------------------------------------------

def _clear_jobs(prefix: str):
    for job in scheduler.get_jobs():
        if job.id.startswith(prefix):
            try:
                job.remove()
            except Exception:  # already gone
                pass


def refresh_next_run(group_id: int | None = None, queue_id: int | None = None):
    """Copy the live job's next fire time onto the row, for display.

    Deliberately does not touch the jobs themselves: re-adding an interval job
    would re-anchor it to now and slowly drift the schedule every post.
    """
    if group_id is not None:
        prefix, model, key = f"{GROUP_PREFIX}{group_id}:", ContentGroup, group_id
    elif queue_id is not None:
        prefix, model, key = f"{QUEUE_PREFIX}{queue_id}:", Queue, queue_id
    else:
        return
    next_run = _next_run_for(prefix)
    db = SessionLocal()
    try:
        row = db.get(model, key)
        if row:
            row.next_run_at = (
                next_run.astimezone(timezone.utc).replace(tzinfo=None) if next_run else None
            )
            db.commit()
    finally:
        db.close()


def _job_next_run(job):
    """A job added before the scheduler starts has no next run time yet."""
    return getattr(job, "next_run_time", None)


def _next_run_for(prefix: str):
    runs = [
        _job_next_run(job)
        for job in scheduler.get_jobs()
        if job.id.startswith(prefix) and _job_next_run(job)
    ]
    return min(runs) if runs else None


def sync_group(group_id: int) -> datetime | None:
    """Make the scheduler match the group row. Returns the next run, if any."""
    prefix = f"{GROUP_PREFIX}{group_id}:"
    _clear_jobs(prefix)

    db = SessionLocal()
    try:
        group = db.get(ContentGroup, group_id)
        if not group:
            return None
        active = group.status == QUEUED
        triggers = build_triggers(group) if active else []
        for index, trigger in enumerate(triggers):
            scheduler.add_job(
                run_group_job, trigger, args=[group_id],
                id=f"{prefix}{index}", replace_existing=True,
            )
        next_run = _next_run_for(prefix)
        group.next_run_at = next_run.astimezone(timezone.utc).replace(tzinfo=None) if next_run else None
        db.commit()
        return next_run
    finally:
        db.close()


def sync_queue(queue_id: int) -> datetime | None:
    prefix = f"{QUEUE_PREFIX}{queue_id}:"
    _clear_jobs(prefix)

    db = SessionLocal()
    try:
        queue = db.get(Queue, queue_id)
        if not queue:
            return None
        active = queue.status == QUEUED
        triggers = build_triggers(queue) if active else []
        for index, trigger in enumerate(triggers):
            scheduler.add_job(
                run_queue_job, trigger, args=[queue_id],
                id=f"{prefix}{index}", replace_existing=True,
            )
        next_run = _next_run_for(prefix)
        queue.next_run_at = next_run.astimezone(timezone.utc).replace(tzinfo=None) if next_run else None
        db.commit()
        return next_run
    finally:
        db.close()


def resync_all():
    """Re-register every active schedule from the database.

    Job pickles from older versions point at functions that have since moved,
    so rather than trusting whatever the jobstore holds we rebuild the whole
    set from the rows, which are the real source of truth.
    """
    db = SessionLocal()
    try:
        group_ids = [g.id for g in db.query(ContentGroup.id).filter(ContentGroup.status == QUEUED).all()]
        queue_ids = [q.id for q in db.query(Queue.id).filter(Queue.status == QUEUED).all()]
    finally:
        db.close()

    for gid in group_ids:
        sync_group(gid)
    for qid in queue_ids:
        sync_queue(qid)

    # Anything left over refers to a row that is no longer active.
    known = {f"{GROUP_PREFIX}{gid}:" for gid in group_ids} | {f"{QUEUE_PREFIX}{qid}:" for qid in queue_ids}
    for job in scheduler.get_jobs():
        if job.id.startswith(DELETE_PREFIX):
            continue
        if not any(job.id.startswith(prefix) for prefix in known):
            try:
                job.remove()
            except Exception:
                pass

    log.info(
        "Schedules active: %d group(s), %d queue(s), %d job(s)",
        len(group_ids), len(queue_ids), len(scheduler.get_jobs()),
    )


def job_report() -> list[str]:
    jobs = [(job, _job_next_run(job)) for job in scheduler.get_jobs()]
    far_future = datetime.max.replace(tzinfo=timezone.utc)
    jobs.sort(key=lambda pair: pair[1] or far_future)
    return [
        f"{job.id} → {when.strftime('%Y-%m-%d %H:%M:%S %Z') if when else 'pending'}"
        for job, when in jobs
    ]


# --------------------------------------------------------------------------
# Delivery
# --------------------------------------------------------------------------

def _keyboard(group: ContentGroup) -> InlineKeyboardMarkup | None:
    rows = []
    for row in group.buttons():
        built = [
            InlineKeyboardButton(text=b["text"], url=b["url"])
            for b in row
            if b.get("text") and b.get("url")
        ]
        if built:
            rows.append(built)
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def _partition(items: list[tuple]) -> list[tuple[str, list[tuple]]]:
    """Split media into the largest batches Telegram will accept.

    Photos and videos share an album; audio and documents each album only
    among themselves; anything else has to be sent on its own.
    """
    batches: list[tuple[str, list[tuple]]] = []
    current_family = None
    current: list[tuple] = []

    def flush():
        nonlocal current, current_family
        if current:
            batches.append(("album" if len(current) > 1 else "solo", current))
        current, current_family = [], None

    for item in items:
        family = ALBUM_FAMILIES.get(item[1])
        if family is None:
            flush()
            batches.append(("solo", [item]))
            continue
        if family != current_family or len(current) >= config.ALBUM_LIMIT:
            flush()
            current_family = family
        current.append(item)
    flush()
    return batches


async def _call(func, *args, **kwargs):
    """Invoke a Bot method, honouring flood limits and retrying network blips."""
    last: Exception | None = None
    for attempt in range(config.SEND_RETRIES):
        try:
            return await func(*args, **kwargs)
        except TelegramRetryAfter as exc:
            wait = min(exc.retry_after, config.MAX_RETRY_AFTER)
            if exc.retry_after > config.MAX_RETRY_AFTER:
                raise
            log.warning("Flood limit hit, sleeping %ss", wait)
            await asyncio.sleep(wait + 1)
            last = exc
        except TelegramNetworkError as exc:
            last = exc
            await asyncio.sleep(2 ** attempt)
    raise last if last else RuntimeError("send failed")


async def _send_solo(chat_id, thread_id, media_type, file_id, caption, parse_mode,
                     markup, group: ContentGroup):
    assert _bot is not None
    common = {
        "chat_id": chat_id,
        "message_thread_id": thread_id,
        "disable_notification": bool(group.silent),
        "protect_content": bool(group.protect_content),
    }
    captioned = {"caption": caption or None, "parse_mode": parse_mode, "reply_markup": markup}
    senders = {
        "photo": (_bot.send_photo, "photo", True),
        "video": (_bot.send_video, "video", True),
        "animation": (_bot.send_animation, "animation", True),
        "document": (_bot.send_document, "document", True),
        "audio": (_bot.send_audio, "audio", True),
        "voice": (_bot.send_voice, "voice", True),
        "video_note": (_bot.send_video_note, "video_note", False),
        "sticker": (_bot.send_sticker, "sticker", False),
    }
    func, kwarg, supports_caption = senders.get(media_type, (_bot.send_document, "document", True))
    payload = dict(common, **{kwarg: file_id})
    if supports_caption:
        payload.update(captioned)
    else:
        # Video notes and stickers carry no caption, so the keyboard rides along
        # but the text has to follow in its own message.
        payload["reply_markup"] = markup
    return await _call(func, **payload)


async def _deliver_to_chat(group: ContentGroup, items: list[tuple], chat_id: str,
                           thread_id: int | None) -> list[int]:
    """Send one group to one chat. Returns the message ids created."""
    assert _bot is not None
    parse_mode = PARSE_MODES.get(group.caption_mode or "HTML", ParseMode.HTML)
    caption = group.caption or ""
    markup = _keyboard(group)
    message_ids: list[int] = []

    if not items:
        if not caption:
            raise ValueError("group has neither media nor a caption")
        sent = await _call(
            _bot.send_message, chat_id=chat_id, text=caption, parse_mode=parse_mode,
            message_thread_id=thread_id, reply_markup=markup,
            disable_notification=bool(group.silent),
            protect_content=bool(group.protect_content),
        )
        return [sent.message_id]

    batches = _partition(items)
    caption_pending = bool(caption)
    markup_pending = markup is not None
    # A lone item can carry both the caption and the keyboard itself, which is
    # the tidiest possible post; anything larger needs a follow-up message
    # because Telegram forbids keyboards on albums.
    single = len(batches) == 1 and batches[0][0] == "solo"

    for kind, batch in batches:
        if kind == "solo":
            file_id, media_type = batch[0]
            sent = await _send_solo(
                chat_id, thread_id, media_type, file_id,
                caption if caption_pending else None, parse_mode,
                markup if (single and markup_pending) else None, group,
            )
            message_ids.append(sent.message_id)
            if caption_pending and media_type not in ("video_note", "sticker"):
                caption_pending = False
            if single and markup_pending:
                markup_pending = False
        else:
            media = []
            for index, (file_id, media_type) in enumerate(batch):
                fields = {"media": file_id}
                if index == 0 and caption_pending:
                    fields["caption"] = caption
                    # Always explicit: omitting it would let the bot-wide HTML
                    # default apply even when the group asked for no parsing.
                    fields["parse_mode"] = parse_mode
                media.append(_INPUT_MEDIA[media_type](**fields))
            sent = await _call(
                _bot.send_media_group, chat_id=chat_id, media=media,
                message_thread_id=thread_id,
                disable_notification=bool(group.silent),
                protect_content=bool(group.protect_content),
            )
            message_ids.extend(m.message_id for m in sent)
            caption_pending = False

    if markup_pending or (caption_pending and caption):
        text = caption if caption_pending else "⁣"  # invisible separator
        sent = await _call(
            _bot.send_message, chat_id=chat_id, text=text, parse_mode=parse_mode,
            message_thread_id=thread_id, reply_markup=markup,
            disable_notification=True, protect_content=bool(group.protect_content),
        )
        message_ids.append(sent.message_id)

    return message_ids


def _load_group_snapshot(db, group_id: int):
    """Read a group and its media while the session is open.

    ORM attributes stop working once the session closes, so anything the send
    path needs is copied out here.
    """
    group = db.get(ContentGroup, group_id)
    if not group:
        return None, [], []
    items = [
        (m.file_id, m.media_type)
        for m in db.query(MediaItem)
        .filter(MediaItem.group_id == group_id)
        .order_by(MediaItem.position, MediaItem.id)
        .all()
    ]
    targets = [
        (t.chat_id, t.thread_id)
        for t in db.query(Target)
        .filter(Target.group_id == group_id, Target.enabled.is_(True))
        .order_by(Target.id)
        .all()
    ]
    if not targets and group.target_chat_id:
        targets = [(group.target_chat_id, None)]
    return group, items, targets


def in_quiet_hours(obj, now_local: datetime | None = None) -> bool:
    start, end = obj.quiet_start, obj.quiet_end
    if start is None or end is None or start == end:
        return False
    tz = config.tz_or_default(obj.timezone_name)
    now_local = now_local or datetime.now(tz)
    minute = now_local.hour * 60 + now_local.minute
    if start < end:
        return start <= minute < end
    # Window wraps past midnight, e.g. 23:00 → 07:00.
    return minute >= start or minute < end


class PostResult(NamedTuple):
    ok: int                                  # chats delivered to
    errors: list[str]
    messages: list[tuple[str, list[int]]]    # (chat_id, message ids) per chat


async def post_group(group_id: int, trigger: str = "manual",
                     override_chat: str | None = None,
                     count_it: bool = True,
                     queue_id: int | None = None) -> PostResult:
    """Deliver a group to its targets, recording what was sent where."""
    db = SessionLocal()
    try:
        group, items, targets = _load_group_snapshot(db, group_id)
        if not group:
            return PostResult(0, ["group no longer exists"], [])
        db.expunge(group)
    finally:
        db.close()

    if override_chat:
        targets = [(override_chat, None)]
    if not targets:
        return PostResult(0, ["no target chat set"], [])
    if not items and not group.caption:
        return PostResult(0, ["nothing to post — add media or a caption"], [])

    if group.shuffle:
        items = list(items)
        random.shuffle(items)

    if group.rotate_targets and len(targets) > 1 and not override_chat:
        index = (group.rotation_index or 0) % len(targets)
        chosen = [targets[index]]
        _advance_rotation(group_id, index + 1)
        targets = chosen

    ok_count, errors, sent = 0, [], []
    for chat_id, thread_id in targets:
        try:
            message_ids = await _deliver_to_chat(group, items, chat_id, thread_id)
        except (TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter,
                TelegramNetworkError, ValueError) as exc:
            errors.append(f"{chat_id}: {exc}")
            _log_post(group_id, queue_id, chat_id, [], len(items), False, str(exc), trigger)
            continue

        ok_count += 1
        sent.append((chat_id, message_ids))
        _log_post(group_id, queue_id, chat_id, message_ids, len(items), True, None, trigger)

        if group.pin_post and message_ids:
            try:
                await _call(_bot.pin_chat_message, chat_id=chat_id,
                            message_id=message_ids[0], disable_notification=True)
            except Exception as exc:  # pinning is a nicety, never fatal
                log.warning("Pin failed in %s: %s", chat_id, exc)

        if group.delete_after and message_ids:
            schedule_deletion(chat_id, message_ids, group.delete_after)

    _finalize_run(group_id, ok_count, errors, trigger, count_it)
    return PostResult(ok_count, errors, sent)


def _advance_rotation(group_id: int, next_index: int):
    db = SessionLocal()
    try:
        group = db.get(ContentGroup, group_id)
        if group:
            group.rotation_index = next_index
            db.commit()
    finally:
        db.close()


def _log_post(group_id, queue_id, chat_id, message_ids, item_count, ok, error, trigger):
    db = SessionLocal()
    try:
        db.add(PostLog(
            group_id=group_id, queue_id=queue_id, chat_id=str(chat_id),
            message_ids=json.dumps(message_ids), item_count=item_count,
            ok=ok, error=error, trigger=trigger,
        ))
        db.commit()
    except Exception as exc:
        log.warning("Could not write post log: %s", exc)
    finally:
        db.close()


def _finalize_run(group_id: int, ok_count: int, errors: list[str], trigger: str, count_it: bool):
    """Update counters and retire the schedule if the group is finished."""
    retire = False
    db = SessionLocal()
    try:
        group = db.get(ContentGroup, group_id)
        if not group:
            return
        group.last_run_at = utcnow()
        group.last_error = "; ".join(errors)[:2000] if errors else None
        if ok_count and count_it:
            group.posts_sent = (group.posts_sent or 0) + 1
        if trigger == "manual" and group.status == DRAFT and ok_count:
            group.status = POSTED
        if group.max_posts and (group.posts_sent or 0) >= group.max_posts:
            group.status = DONE
            retire = True
        if group.schedule_kind == KIND_ONCE and trigger == "schedule":
            group.status = DONE
            retire = True
        owner_id, notify = group.owner_id, group.notify_owner
        label = group.label()
        db.commit()
    finally:
        db.close()

    if retire:
        sync_group(group_id)
    else:
        refresh_next_run(group_id=group_id)

    if errors and notify and owner_id and _bot:
        asyncio.create_task(_notify(
            owner_id,
            f"⚠️ <b>{esc(label)}</b> failed to post:\n<code>{esc(errors[0][:300])}</code>",
        ))


async def _notify(user_id: int, text: str):
    try:
        await _bot.send_message(user_id, text)
    except Exception as exc:
        log.warning("Could not notify %s: %s", user_id, exc)


async def run_group_job(group_id: int):
    """Scheduler entrypoint for a group's own recurring post."""
    db = SessionLocal()
    try:
        group = db.get(ContentGroup, group_id)
        if not group or group.status != QUEUED:
            return
        skip = in_quiet_hours(group)
        label = group.label()
    finally:
        db.close()

    if skip:
        log.info("Skipping %s — inside quiet hours", label)
        return

    result = await post_group(group_id, trigger="schedule")
    log.info("Scheduled post %s: %d ok, %d error(s)", label, result.ok, len(result.errors))


# --------------------------------------------------------------------------
# Queues
# --------------------------------------------------------------------------

def queue_group_ids(db, queue_id: int) -> list[int]:
    return [
        qi.group_id
        for qi in db.query(QueueItem)
        .filter(QueueItem.queue_id == queue_id)
        .order_by(QueueItem.position, QueueItem.id)
        .all()
    ]


async def run_queue_job(queue_id: int):
    """Post the next group in a queue and advance the cursor."""
    db = SessionLocal()
    try:
        queue = db.get(Queue, queue_id)
        if not queue or queue.status != QUEUED:
            return
        if in_quiet_hours(queue):
            log.info("Skipping queue %s — inside quiet hours", queue.label())
            return
        group_ids = queue_group_ids(db, queue_id)
        if not group_ids:
            return
        cursor = (queue.cursor or 0) % len(group_ids)
        group_id = random.choice(group_ids) if queue.shuffle else group_ids[cursor]
        delete_previous = queue.delete_previous
        label = queue.label()
    finally:
        db.close()

    if delete_previous:
        await _delete_last_queue_post(queue_id)

    result = await post_group(group_id, trigger="queue", queue_id=queue_id)
    ok, errors = result.ok, result.errors

    finished = False
    db = SessionLocal()
    try:
        queue = db.get(Queue, queue_id)
        if queue:
            queue.last_message_ids = json.dumps(result.messages) if result.messages else None
            next_cursor = cursor + 1
            if next_cursor >= len(group_ids):
                if queue.loop:
                    next_cursor = 0
                else:
                    queue.status = DONE
                    finished = True
                    next_cursor = 0
            queue.cursor = next_cursor
            queue.posts_sent = (queue.posts_sent or 0) + (1 if ok else 0)
            queue.last_run_at = utcnow()
            queue.last_error = "; ".join(errors)[:2000] if errors else None
            db.commit()
    finally:
        db.close()

    if finished:
        sync_queue(queue_id)
    else:
        refresh_next_run(queue_id=queue_id)
    log.info("Queue %s posted group #%d (%d ok)%s", label, group_id, ok,
             " — queue finished" if finished else "")


async def _delete_last_queue_post(queue_id: int):
    """Remove what the previous tick sent, so the queue reads as one rolling post."""
    db = SessionLocal()
    try:
        queue = db.get(Queue, queue_id)
        raw = queue.last_message_ids if queue else None
    finally:
        db.close()

    if not raw:
        return
    try:
        payload = json.loads(raw)
    except ValueError:
        return

    for chat_id, message_ids in payload:
        for message_id in message_ids:
            try:
                await _bot.delete_message(chat_id, message_id)
            except Exception:
                # Already gone, too old for a bot to delete, or rights revoked.
                pass


# --------------------------------------------------------------------------
# Auto-delete
# --------------------------------------------------------------------------

def schedule_deletion(chat_id: str, message_ids: list[int], after_seconds: int):
    run_at = datetime.now(timezone.utc).timestamp() + after_seconds
    scheduler.add_job(
        delete_messages_job,
        DateTrigger(run_date=datetime.fromtimestamp(run_at, tz=timezone.utc)),
        args=[str(chat_id), json.dumps(message_ids)],
        id=f"{DELETE_PREFIX}{chat_id}:{message_ids[0]}",
        replace_existing=True,
    )


async def delete_messages_job(chat_id: str, ids_json: str):
    try:
        ids = json.loads(ids_json)
    except ValueError:
        return
    for message_id in ids:
        try:
            await _bot.delete_message(chat_id, message_id)
        except Exception:
            pass


# --------------------------------------------------------------------------
# Broadcast
# --------------------------------------------------------------------------

async def broadcast(owner_id: int, text: str) -> tuple[int, list[str]]:
    """Send one message to every distinct target the owner has configured."""
    db = SessionLocal()
    try:
        chats = {
            t.chat_id
            for t in db.query(Target)
            .join(ContentGroup, Target.group_id == ContentGroup.id)
            .filter(ContentGroup.owner_id == owner_id, Target.enabled.is_(True))
            .all()
        }
    finally:
        db.close()

    ok, errors = 0, []
    for chat_id in sorted(chats):
        try:
            await _call(_bot.send_message, chat_id=chat_id, text=text)
            ok += 1
        except Exception as exc:
            errors.append(f"{chat_id}: {exc}")
    return ok, errors


__all__ = [
    "scheduler", "attach_bot", "sync_group", "sync_queue", "resync_all",
    "post_group", "run_group_job", "run_queue_job", "broadcast",
    "describe_schedule", "validate_cron", "job_report", "in_quiet_hours",
    "queue_group_ids", "refresh_next_run", "build_triggers", "PARSE_MODES",
]
