"""Queues: rotating a list of groups on one shared schedule."""

import logging
import time

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import keyboards as kb
import views
from access import get_prefs
from db import (
    DRAFT, KIND_INTERVAL, PAUSED, QUEUED, ContentGroup, Queue, QueueItem,
    SessionLocal,
)
from handlers.ui import ask, show_view, swap
from scheduling import build_triggers, run_queue_job, sync_queue
from utils import format_duration

log = logging.getLogger(__name__)
router = Router(name="queues")

QUEUE_TOGGLES = {"loop", "shuffle", "delete_previous"}


def _owned(db, queue_id: int, user_id: int) -> Queue | None:
    queue = db.get(Queue, queue_id)
    return queue if queue and queue.owner_id == user_id else None


def create_queue(user_id: int, name: str | None = None) -> int:
    prefs = get_prefs(user_id)
    db = SessionLocal()
    try:
        queue = Queue(
            owner_id=user_id, name=name, status=DRAFT,
            schedule_kind=KIND_INTERVAL,
            interval_seconds=prefs.default_interval or 3600,
            timezone_name=prefs.timezone_name,
            loop=True,
        )
        db.add(queue)
        db.commit()
        return queue.id
    finally:
        db.close()


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

@router.message(Command("queues"))
async def cmd_queues(message: Message):
    await show_view(message, "nav:queues", message.from_user.id)


@router.message(Command("newqueue"))
async def cmd_new_queue(message: Message, command):
    qid = create_queue(message.from_user.id, (command.args or "").strip() or None)
    await message.answer(f"✅ Created queue <code>#{qid}</code>.")
    await show_view(message, f"q:{qid}:menu", message.from_user.id)


@router.message(Command("queue"))
async def cmd_queue(message: Message, command):
    qid = _arg_int(command.args)
    if qid is None:
        return await message.answer("Usage: <code>/queue 3</code>")
    await show_view(message, f"q:{qid}:menu", message.from_user.id)


@router.message(Command("qadd", "qdel"))
async def cmd_queue_membership(message: Message, command):
    parts = (command.args or "").split()
    if len(parts) != 2 or not all(p.lstrip("#").isdigit() for p in parts):
        return await message.answer(
            f"Usage: <code>/{command.command} &lt;queue id&gt; &lt;group id&gt;</code>"
        )
    qid, gid = (int(p.lstrip("#")) for p in parts)
    if command.command == "qadd":
        ok, note = add_to_queue(qid, gid, message.from_user.id)
    else:
        ok, note = remove_from_queue(qid, gid, message.from_user.id)
    await message.answer(("✅ " if ok else "⚠️ ") + note)
    if ok:
        await show_view(message, f"q:{qid}:items", message.from_user.id)


@router.message(Command("qstart", "qstop"))
async def cmd_queue_run(message: Message, command):
    qid = _arg_int(command.args)
    if qid is None:
        return await message.answer(f"Usage: <code>/{command.command} 3</code>")
    status = QUEUED if command.command == "qstart" else PAUSED
    ok, note = _set_status(qid, message.from_user.id, status)
    await message.answer(("✅ " if ok else "⚠️ ") + note)


@router.message(Command("qnext"))
async def cmd_queue_next(message: Message, command):
    qid = _arg_int(command.args)
    if qid is None:
        return await message.answer("Usage: <code>/qnext 3</code>")
    await message.answer("⏭ Posting the next item…")
    await run_queue_job(qid)
    await show_view(message, f"q:{qid}:menu", message.from_user.id)


def _arg_int(args: str | None) -> int | None:
    raw = (args or "").strip().lstrip("#")
    return int(raw) if raw.isdigit() else None


# --------------------------------------------------------------------------
# Membership
# --------------------------------------------------------------------------

def add_to_queue(queue_id: int, group_id: int, user_id: int) -> tuple[bool, str]:
    db = SessionLocal()
    try:
        if not _owned(db, queue_id, user_id):
            return False, "No such queue."
        group = db.get(ContentGroup, group_id)
        if not group or group.owner_id != user_id:
            return False, "No such group."
        if db.query(QueueItem).filter(
            QueueItem.queue_id == queue_id, QueueItem.group_id == group_id
        ).first():
            return False, "That group is already in the rota."
        last = (
            db.query(QueueItem).filter(QueueItem.queue_id == queue_id)
            .order_by(QueueItem.position.desc()).first()
        )
        db.add(QueueItem(
            queue_id=queue_id, group_id=group_id,
            position=(last.position or 0) + 1 if last else 1,
        ))
        db.commit()
        return True, f"Added {group.label()} to the rota."
    finally:
        db.close()


def remove_from_queue(queue_id: int, group_id: int, user_id: int) -> tuple[bool, str]:
    db = SessionLocal()
    try:
        if not _owned(db, queue_id, user_id):
            return False, "No such queue."
        row = db.query(QueueItem).filter(
            QueueItem.queue_id == queue_id, QueueItem.group_id == group_id
        ).first()
        if not row:
            return False, "That group is not in the rota."
        db.delete(row)
        db.commit()
        return True, "Removed from the rota."
    finally:
        db.close()


# --------------------------------------------------------------------------
# Callbacks
# --------------------------------------------------------------------------

@router.callback_query(F.data == "nav:newqueue")
async def cb_new_queue(call: CallbackQuery):
    qid = create_queue(call.from_user.id)
    await show_view(call, f"q:{qid}:menu", call.from_user.id)
    await call.answer("Queue created.")


@router.callback_query(F.data.startswith("q:"))
async def cb_queue(call: CallbackQuery, state: FSMContext):
    parts = call.data.split(":")
    qid, action = int(parts[1]), parts[2]
    extra = parts[3] if len(parts) > 3 else None
    user_id = call.from_user.id

    db = SessionLocal()
    try:
        if not _owned(db, qid, user_id):
            return await call.answer("No such queue.", show_alert=True)
    finally:
        db.close()

    if action in {"menu", "items"}:
        await show_view(call, f"q:{qid}:{action}", user_id)
        return await call.answer()

    if action == "rename":
        return await ask(call, state, "q", qid, "name", f"q:{qid}:menu")

    if action == "set" and extra:
        return await ask(call, state, "q", qid, extra, f"q:{qid}:menu")

    if action == "opt" and extra in QUEUE_TOGGLES:
        value = _toggle(qid, extra)
        await show_view(call, f"q:{qid}:menu", user_id)
        return await call.answer(f"{extra.replace('_', ' ')}: {'on' if value else 'off'}")

    if action == "additem":
        db = SessionLocal()
        try:
            member_ids = {
                qi.group_id for qi in
                db.query(QueueItem).filter(QueueItem.queue_id == qid).all()
            }
            groups = [
                (g.id, g.label())
                for g in db.query(ContentGroup)
                .filter(ContentGroup.owner_id == user_id)
                .order_by(ContentGroup.id.desc()).limit(30).all()
                if g.id not in member_ids
            ]
        finally:
            db.close()
        if not groups:
            return await call.answer("No groups left to add.", show_alert=True)
        await swap(
            call,
            "➕ <b>Add a group to the rota</b>\n\nIts own schedule is ignored while "
            "the queue drives it; the queue's interval decides when it posts.",
            kb.pick_group_menu(groups, f"qpick:{qid}"),
        )
        return await call.answer()

    if action == "step":
        await call.answer("Posting the next item…")
        await run_queue_job(qid)
        return await show_view(call, f"q:{qid}:menu", user_id)

    if action == "resetcursor":
        _update(qid, cursor=0)
        await show_view(call, f"q:{qid}:menu", user_id)
        return await call.answer("Cursor reset to the top.")

    if action in {"activate", "pause"}:
        ok, note = _set_status(qid, user_id, QUEUED if action == "activate" else PAUSED)
        await show_view(call, f"q:{qid}:menu", user_id)
        return await call.answer(note, show_alert=not ok)

    if action == "delete":
        await swap(
            call, "Delete this queue? The groups in it are kept.",
            kb.confirm("delqueue", str(qid), f"q:{qid}:menu"),
        )
        return await call.answer()

    await call.answer()


@router.callback_query(F.data.startswith("qpick:"))
async def cb_queue_pick(call: CallbackQuery):
    _, raw_qid, raw_gid = call.data.split(":")
    ok, note = add_to_queue(int(raw_qid), int(raw_gid), call.from_user.id)
    await show_view(call, f"q:{raw_qid}:items", call.from_user.id)
    await call.answer(note, show_alert=not ok)


@router.callback_query(F.data.startswith("qi:"))
async def cb_queue_item(call: CallbackQuery):
    _, raw, action = call.data.split(":")
    item_id = int(raw)

    db = SessionLocal()
    try:
        item = db.get(QueueItem, item_id)
        if not item:
            return await call.answer("Already removed.")
        qid = item.queue_id
        siblings = (
            db.query(QueueItem).filter(QueueItem.queue_id == qid)
            .order_by(QueueItem.position, QueueItem.id).all()
        )
        index = next(i for i, q in enumerate(siblings) if q.id == item_id)
        if action == "del":
            db.delete(item)
        elif action in {"up", "down"}:
            neighbour = index - 1 if action == "up" else index + 1
            if 0 <= neighbour < len(siblings):
                reordered = list(siblings)
                reordered[index], reordered[neighbour] = reordered[neighbour], reordered[index]
                for rank, row in enumerate(reordered, start=1):
                    row.position = rank
        db.commit()
    finally:
        db.close()

    text, markup = views.queue_items_view(qid)
    await swap(call, text, markup)
    await call.answer("Removed." if action == "del" else "Moved.")


@router.callback_query(F.data.startswith("ok:delqueue:"))
async def cb_confirm_delete_queue(call: CallbackQuery):
    qid = int(call.data.split(":")[2])
    db = SessionLocal()
    try:
        queue = _owned(db, qid, call.from_user.id)
        if queue:
            db.query(QueueItem).filter(QueueItem.queue_id == qid).delete()
            db.delete(queue)
            db.commit()
    finally:
        db.close()
    sync_queue(qid)
    await show_view(call, "nav:queues", call.from_user.id)
    await call.answer("Deleted.")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _update(queue_id: int, **fields):
    db = SessionLocal()
    try:
        queue = db.get(Queue, queue_id)
        if not queue:
            return
        for key, value in fields.items():
            setattr(queue, key, value)
        db.commit()
    finally:
        db.close()


def _toggle(queue_id: int, field: str) -> bool:
    db = SessionLocal()
    try:
        queue = db.get(Queue, queue_id)
        if not queue:
            return False
        value = not bool(getattr(queue, field))
        setattr(queue, field, value)
        db.commit()
        return value
    finally:
        db.close()


def _set_status(queue_id: int, user_id: int, status: str) -> tuple[bool, str]:
    db = SessionLocal()
    try:
        queue = _owned(db, queue_id, user_id)
        if not queue:
            return False, "No such queue."
        if status == QUEUED:
            count = db.query(QueueItem).filter(QueueItem.queue_id == queue_id).count()
            if not count:
                return False, "Add at least one group to the rota first."
            if not build_triggers(queue):
                return False, "Schedule is incomplete — set an interval, cron or daily time."
        queue.status = status
        db.commit()
    finally:
        db.close()

    next_run = sync_queue(queue_id)
    if status != QUEUED:
        return True, "⏸ Paused."
    if not next_run:
        return True, "▶️ Active."
    return True, f"▶️ Active. Next item in {format_duration(int(next_run.timestamp() - time.time()))}."
