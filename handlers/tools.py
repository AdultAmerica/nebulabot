"""Settings, diagnostics, backup/restore, admin management, broadcast."""

import json
import logging
import os
import platform
import time
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

import config
import keyboards as kb
import views
from access import add_admin, get_prefs, is_root, remove_admin, set_pref, user_tz
from db import (
    PAUSED, QUEUED, ContentGroup, MediaItem, PostLog, Queue, QueueItem,
    SessionLocal, Target,
)
from handlers.ui import Form, ask, show_view, swap
from scheduling import (
    album_keyboard_supported, broadcast, scheduler, sync_group, sync_queue,
)
from utils import esc, format_duration, rich_text, truncate

log = logging.getLogger(__name__)
router = Router(name="tools")

STARTED_AT = time.time()

EXPORT_FIELDS = (
    "name", "tags", "caption", "caption_mode", "buttons_json", "status",
    "schedule_kind", "interval_seconds", "cron_expr", "daily_times",
    "jitter_seconds", "timezone_name", "quiet_start", "quiet_end", "max_posts",
    "shuffle", "rotate_targets", "silent", "protect_content", "pin_post",
    "delete_after", "notify_owner", "buttons_attach",
)


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

@router.message(Command("settings"))
async def cmd_settings(message: Message):
    await show_view(message, "nav:settings", message.from_user.id)


@router.message(Command("timezone"))
async def cmd_timezone(message: Message, command):
    name = (command.args or "").strip()
    if not name:
        current = get_prefs(message.from_user.id).timezone_name or "UTC"
        return await message.answer(
            f"Your timezone is <code>{esc(current)}</code>.\n"
            "Change it with <code>/timezone Europe/Berlin</code>."
        )
    canonical = config.validate_timezone(name)
    if not canonical:
        return await message.answer("Unknown timezone. Use an IANA name like <code>Asia/Tokyo</code>.")
    set_pref(message.from_user.id, "timezone_name", canonical)
    await message.answer(f"✅ Timezone set to <code>{esc(canonical)}</code>.")


@router.callback_query(F.data.startswith("s:"))
async def cb_settings(call: CallbackQuery, state: FSMContext):
    parts = call.data.split(":")
    action = parts[1]
    user_id = call.from_user.id

    if action == "toggle":
        field = parts[2]
        prefs = get_prefs(user_id)
        set_pref(user_id, field, not bool(getattr(prefs, field)))
        await show_view(call, "nav:settings", user_id)
        return await call.answer()

    field = {"tz": "tz", "interval": "default_interval", "target": "default_target"}.get(action)
    if not field:
        return await call.answer()
    await ask(call, state, "s", user_id, field, "nav:settings")


# --------------------------------------------------------------------------
# Diagnostics
# --------------------------------------------------------------------------

@router.message(Command("stats"))
async def cmd_stats(message: Message):
    await show_view(message, "nav:stats", message.from_user.id)


@router.message(Command("history", "logs"))
async def cmd_history(message: Message):
    text, markup = views.history_view(message.from_user.id)
    await message.answer(text, reply_markup=markup)


@router.message(Command("jobs"))
async def cmd_jobs(message: Message):
    text, markup = views.jobs_view()
    await message.answer(text, reply_markup=markup)


@router.message(Command("health"))
async def cmd_health(message: Message):
    await message.answer(_health_text(message.from_user.id))


def _health_text(user_id: int) -> str:
    tz = user_tz(user_id)
    db_path = config.DATABASE_URL.replace("sqlite:///", "")
    size = os.path.getsize(db_path) / 1024 if os.path.exists(db_path) else 0
    db = SessionLocal()
    try:
        counts = {
            "groups": db.query(ContentGroup).count(),
            "queues": db.query(Queue).count(),
            "media": db.query(MediaItem).count(),
            "targets": db.query(Target).count(),
            "deliveries": db.query(PostLog).count(),
        }
        failing = (
            db.query(ContentGroup)
            .filter(ContentGroup.status == QUEUED, ContentGroup.last_error.isnot(None))
            .count()
        )
    finally:
        db.close()

    album_kb = {True: "yes", False: "no", None: "not yet probed"}[
        album_keyboard_supported()
    ]
    uptime = int(time.time() - STARTED_AT)
    return (
        "❤️ <b>Health</b>\n\n"
        f"Version: <b>{config.VERSION}</b> · Python {platform.python_version()}\n"
        f"Mode: {'webhook' if config.WEBHOOK_URL else 'polling'}\n"
        f"Uptime: {format_duration(uptime)}\n"
        f"Scheduler: {'running' if scheduler.running else 'stopped'} · "
        f"{len(scheduler.get_jobs())} job(s)\n"
        f"Keyboard on albums: {album_kb}\n"
        f"Server time: {esc(datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))}\n"
        f"Your time: {esc(datetime.now(tz).strftime('%Y-%m-%d %H:%M %Z'))}\n\n"
        + " · ".join(f"{key}: <b>{value}</b>" for key, value in counts.items())
        + f"\nDatabase: {size:.0f} KiB\n"
        + (f"\n⚠️ {failing} active group(s) reported an error on their last run."
           if failing else "\n✅ No errors on the last runs.")
    )


# --------------------------------------------------------------------------
# Bulk schedule control
# --------------------------------------------------------------------------

@router.message(Command("pauseall", "resumeall"))
async def cmd_pause_all(message: Message, command):
    changed = _bulk_status(message.from_user.id, command.command == "pauseall")
    verb = "Paused" if command.command == "pauseall" else "Resumed"
    await message.answer(f"⏯ {verb} {changed} schedule(s).")


def _bulk_status(user_id: int, pause: bool) -> int:
    source, destination = (QUEUED, PAUSED) if pause else (PAUSED, QUEUED)
    db = SessionLocal()
    try:
        groups = db.query(ContentGroup).filter(
            ContentGroup.owner_id == user_id, ContentGroup.status == source
        ).all()
        queues = db.query(Queue).filter(
            Queue.owner_id == user_id, Queue.status == source
        ).all()
        group_ids = [g.id for g in groups]
        queue_ids = [q.id for q in queues]
        for row in groups + queues:
            row.status = destination
        db.commit()
    finally:
        db.close()

    for gid in group_ids:
        sync_group(gid)
    for qid in queue_ids:
        sync_queue(qid)
    return len(group_ids) + len(queue_ids)


# --------------------------------------------------------------------------
# Export / import / backup
# --------------------------------------------------------------------------

@router.message(Command("export"))
async def cmd_export(message: Message, command):
    raw = (command.args or "").strip().lstrip("#")
    group_id = int(raw) if raw.isdigit() else None
    await _send_export(message, message.from_user.id, group_id)


async def _send_export(message: Message, user_id: int, group_id: int | None = None):
    payload = build_export(user_id, group_id)
    if not payload["groups"]:
        return await message.answer("Nothing to export.")
    blob = json.dumps(payload, indent=2, ensure_ascii=False).encode()
    name = f"nebulabot-export-{datetime.now(timezone.utc):%Y%m%d-%H%M}.json"
    await message.answer_document(
        BufferedInputFile(blob, filename=name),
        caption=(
            f"⬇️ {len(payload['groups'])} group(s) exported.\n\n"
            "File IDs are included, so an import into <b>this same bot</b> "
            "restores the media too. A different bot token cannot reuse them — "
            "there the groups arrive with captions, buttons and schedules but "
            "no media."
        ),
    )


def build_export(user_id: int, group_id: int | None = None) -> dict:
    db = SessionLocal()
    try:
        query = db.query(ContentGroup).filter(ContentGroup.owner_id == user_id)
        if group_id is not None:
            query = query.filter(ContentGroup.id == group_id)
        groups = []
        for group in query.order_by(ContentGroup.id).all():
            groups.append({
                "id": group.id,
                **{field: getattr(group, field) for field in EXPORT_FIELDS},
                "media": [
                    {"file_id": m.file_id, "file_unique_id": m.file_unique_id,
                     "media_type": m.media_type, "position": m.position}
                    for m in db.query(MediaItem)
                    .filter(MediaItem.group_id == group.id)
                    .order_by(MediaItem.position, MediaItem.id).all()
                ],
                "targets": [
                    {"chat_id": t.chat_id, "thread_id": t.thread_id, "enabled": t.enabled}
                    for t in db.query(Target).filter(Target.group_id == group.id).all()
                ],
            })
        queues = [
            {
                "name": q.name, "interval_seconds": q.interval_seconds,
                "schedule_kind": q.schedule_kind, "cron_expr": q.cron_expr,
                "daily_times": q.daily_times, "timezone_name": q.timezone_name,
                "loop": q.loop, "shuffle": q.shuffle,
                "group_ids": [
                    qi.group_id for qi in
                    db.query(QueueItem).filter(QueueItem.queue_id == q.id)
                    .order_by(QueueItem.position).all()
                ],
            }
            for q in db.query(Queue).filter(Queue.owner_id == user_id).all()
        ] if group_id is None else []
    finally:
        db.close()
    return {"version": config.VERSION, "exported_at": datetime.now(timezone.utc).isoformat(),
            "groups": groups, "queues": queues}


@router.message(Command("import"))
async def cmd_import(message: Message, state: FSMContext):
    await state.set_state(Form.importing)
    await message.answer(
        "⬆️ <b>Import</b>\n\nSend the <code>.json</code> file exported earlier. "
        "Groups are added alongside your existing ones — nothing is overwritten "
        "or deleted.\n\nSend /cancel to abort.",
        reply_markup=kb.cancel_kb(),
    )


@router.message(StateFilter(Form.importing), F.document)
async def on_import_file(message: Message, state: FSMContext, bot):
    await state.clear()
    if not (message.document.file_name or "").lower().endswith(".json"):
        return await message.answer("That is not a .json file.")
    if message.document.file_size > 4 * 1024 * 1024:
        return await message.answer("That file is too large to import.")

    buffer = await bot.download(message.document)
    try:
        payload = json.loads(buffer.read().decode())
    except (ValueError, UnicodeDecodeError) as exc:
        return await message.answer(f"Could not read that file: <code>{esc(exc)}</code>")

    created, media_count = restore_export(payload, message.from_user.id)
    await message.answer(
        f"✅ Imported {created} group(s) with {media_count} media item(s).\n"
        "They arrive paused — review, then activate."
    )
    await show_view(message, "nav:groups", message.from_user.id)


def restore_export(payload: dict, user_id: int) -> tuple[int, int]:
    groups = payload.get("groups") or []
    created = media_count = 0
    id_map: dict[int, int] = {}

    db = SessionLocal()
    try:
        for entry in groups:
            group = ContentGroup(
                owner_id=user_id,
                **{field: entry.get(field) for field in EXPORT_FIELDS if field != "status"},
                status=PAUSED,
            )
            db.add(group)
            db.commit()
            created += 1
            if entry.get("id"):
                id_map[entry["id"]] = group.id
            for item in entry.get("media") or []:
                db.add(MediaItem(
                    group_id=group.id, file_id=item.get("file_id"),
                    file_unique_id=item.get("file_unique_id"),
                    media_type=item.get("media_type"), position=item.get("position") or 0,
                ))
                media_count += 1
            for target in entry.get("targets") or []:
                db.add(Target(
                    group_id=group.id, chat_id=target.get("chat_id"),
                    thread_id=target.get("thread_id"),
                    enabled=bool(target.get("enabled", True)),
                ))
            db.commit()

        for entry in payload.get("queues") or []:
            queue = Queue(
                owner_id=user_id, name=entry.get("name"), status=PAUSED,
                schedule_kind=entry.get("schedule_kind"),
                interval_seconds=entry.get("interval_seconds"),
                cron_expr=entry.get("cron_expr"), daily_times=entry.get("daily_times"),
                timezone_name=entry.get("timezone_name"),
                loop=bool(entry.get("loop", True)), shuffle=bool(entry.get("shuffle")),
            )
            db.add(queue)
            db.commit()
            for position, old_id in enumerate(entry.get("group_ids") or [], start=1):
                if old_id in id_map:
                    db.add(QueueItem(queue_id=queue.id, group_id=id_map[old_id], position=position))
            db.commit()
    finally:
        db.close()
    return created, media_count


@router.message(Command("backup"))
async def cmd_backup(message: Message):
    if not config.DATABASE_URL.startswith("sqlite"):
        return await message.answer(
            "Backups are only offered for SQLite. Use your database's own tools."
        )
    path = config.DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(path):
        return await message.answer("No database file found yet.")
    with open(path, "rb") as handle:
        blob = handle.read()
    if len(blob) > 45 * 1024 * 1024:
        return await message.answer("The database is too large to send over Telegram.")
    await message.answer_document(
        BufferedInputFile(blob, filename=f"bot-{datetime.now(timezone.utc):%Y%m%d-%H%M}.db"),
        caption=(
            "💾 Full database snapshot: groups, queues, media references, "
            "history and the job store.\n\n"
            "Restore by stopping the bot and putting this file back in its "
            "working directory as <code>bot.db</code>."
        ),
    )


# --------------------------------------------------------------------------
# Admins and broadcast
# --------------------------------------------------------------------------

@router.message(Command("admins"))
async def cmd_admins(message: Message):
    text, markup = views.admins_view()
    await message.answer(text, reply_markup=markup)


@router.message(Command("addadmin", "deladmin"))
async def cmd_admin_change(message: Message, command):
    raw = (command.args or "").strip()
    if not raw.lstrip("-").isdigit():
        return await message.answer(f"Usage: <code>/{command.command} 123456789</code>")
    target = int(raw)
    if command.command == "addadmin":
        ok = add_admin(target, None, message.from_user.id)
        note = f"✅ {target} can now use the bot." if ok else "Already an admin."
    else:
        if is_root(target):
            note = "That admin comes from <code>ADMIN_IDS</code> and must be removed there."
        else:
            ok = remove_admin(target)
            note = f"✅ {target} removed." if ok else "Not an admin."
    await message.answer(note)


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, command):
    text = (command.args or "").strip()
    if not text:
        return await message.answer(
            "Usage: <code>/broadcast Doors open at 8pm</code>\n"
            "Goes once to every enabled target across all your groups."
        )
    body = rich_text(message).split(maxsplit=1)[1]
    ok, errors = await broadcast(message.from_user.id, body)
    note = f"📣 Sent to {ok} chat(s)."
    if errors:
        note += "\n" + "\n".join(f"• <code>{esc(truncate(e, 120))}</code>" for e in errors[:5])
    await message.answer(note)


# --------------------------------------------------------------------------
# Tool callbacks
# --------------------------------------------------------------------------

@router.callback_query(F.data.startswith("x:"))
async def cb_tools(call: CallbackQuery, state: FSMContext):
    parts = call.data.split(":")
    action = parts[1]
    user_id = call.from_user.id

    if action == "jobs":
        text, markup = views.jobs_view()
    elif action == "history":
        text, markup = views.history_view(user_id)
    elif action == "health":
        text, markup = _health_text(user_id), kb.tools_menu()
    elif action == "admins":
        text, markup = views.admins_view()
    elif action == "id":
        text, markup = (
            "🆔 <b>Finding a chat ID</b>\n\n"
            f"Your user ID is <code>{user_id}</code>.\n\n"
            "For a channel: add this bot as an admin, then either run "
            "<code>/id</code> inside the channel or forward one of its messages "
            "to this chat — the reply includes the ID.\n\n"
            "Channel IDs start with <code>-100</code>.",
            kb.tools_menu(),
        )
    elif action == "export":
        await call.answer("Building export…")
        await _send_export(call.message, user_id)
        return
    elif action == "import":
        await state.set_state(Form.importing)
        await swap(
            call,
            "⬆️ <b>Import</b>\n\nSend the <code>.json</code> file exported "
            "earlier. Imported groups arrive paused so you can review them.",
            kb.cancel_kb(),
        )
        return await call.answer()
    elif action == "backup":
        await call.answer("Preparing backup…")
        await cmd_backup(call.message)
        return
    elif action == "broadcast":
        return await ask(call, state, "x", user_id, "broadcast", "nav:tools")
    elif action == "addadmin":
        return await ask(call, state, "x", user_id, "addadmin", "nav:tools")
    elif action == "deladmin":
        target = int(parts[2])
        remove_admin(target)
        text, markup = views.admins_view()
    elif action in {"pauseall", "resumeall"}:
        changed = _bulk_status(user_id, action == "pauseall")
        await call.answer(f"{'Paused' if action == 'pauseall' else 'Resumed'} {changed} schedule(s).",
                          show_alert=True)
        text, markup = "🛠 <b>Tools</b>\n\nDiagnostics, backups and bulk actions.", kb.tools_menu()
    else:
        return await call.answer()

    await swap(call, text, markup)
    await call.answer()
