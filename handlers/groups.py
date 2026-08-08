"""Groups: composition, media, targets, buttons, schedule controls."""

import json
import logging
import time

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import keyboards as kb
import views
from access import get_prefs, user_tz
from db import (
    DRAFT, KIND_INTERVAL, PAUSED, QUEUED, ContentGroup, MediaItem, PostLog,
    Queue, QueueItem, SessionLocal, Target,
)
from forms import apply_group
from handlers.ui import ask, show_view, swap
from scheduling import build_triggers, post_group, sync_group
from utils import esc, format_dt, format_duration, rich_text, truncate

log = logging.getLogger(__name__)
router = Router(name="groups")

MEDIA_KINDS = (
    ("photo", lambda m: m.photo[-1] if m.photo else None),
    ("video", lambda m: m.video),
    ("animation", lambda m: m.animation),
    ("document", lambda m: m.document),
    ("audio", lambda m: m.audio),
    ("voice", lambda m: m.voice),
    ("video_note", lambda m: m.video_note),
    ("sticker", lambda m: m.sticker),
)

MODE_CYCLE = ["HTML", "MarkdownV2", "Markdown", "none"]

TOGGLES = {
    "silent", "protect_content", "pin_post", "shuffle", "rotate_targets",
    "notify_owner", "buttons_attach",
}


def _owned(db, group_id: int, user_id: int) -> ContentGroup | None:
    group = db.get(ContentGroup, group_id)
    if group and group.owner_id == user_id:
        return group
    return None


def create_group(user_id: int, name: str | None = None) -> int:
    """New group seeded from the owner's defaults, so it is usable immediately."""
    prefs = get_prefs(user_id)
    db = SessionLocal()
    try:
        group = ContentGroup(
            owner_id=user_id,
            name=name,
            status=DRAFT,
            schedule_kind=KIND_INTERVAL,
            interval_seconds=prefs.default_interval or 3600,
            timezone_name=prefs.timezone_name,
            caption_mode="HTML",
            notify_owner=bool(prefs.notify_on_error),
        )
        db.add(group)
        db.commit()
        gid = group.id
        if prefs.default_target:
            db.add(Target(group_id=gid, chat_id=prefs.default_target, enabled=True))
            db.commit()
    finally:
        db.close()
    return gid


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

@router.message(Command("new"))
async def cmd_new(message: Message, command, state: FSMContext):
    gid = create_group(message.from_user.id, (command.args or "").strip() or None)
    await state.update_data(current_group=gid)
    await message.answer(
        f"✅ Created group <code>#{gid}</code>. It is now the active group — "
        "any media you send lands here.",
    )
    await show_view(message, f"g:{gid}:menu", message.from_user.id)


@router.message(Command("groups"))
async def cmd_groups(message: Message):
    await show_view(message, "nav:groups", message.from_user.id)


@router.message(Command("group"))
async def cmd_group(message: Message, command, state: FSMContext):
    gid = _arg_int(command.args)
    if gid is None:
        return await message.answer("Usage: <code>/group 12</code>")
    await state.update_data(current_group=gid)
    await show_view(message, f"g:{gid}:menu", message.from_user.id)


@router.message(Command("find"))
async def cmd_find(message: Message, command):
    query = (command.args or "").strip()
    if not query:
        return await message.answer("Usage: <code>/find weekend</code>")
    text, markup = views.group_list_view(message.from_user.id, 0, query)
    await message.answer(text, reply_markup=markup)


@router.message(Command("postnow"))
async def cmd_postnow(message: Message, command):
    gid = _arg_int(command.args)
    if gid is None:
        return await message.answer("Usage: <code>/postnow 12</code>")
    await _do_post(message, gid, message.from_user.id)


@router.message(Command("preview"))
async def cmd_preview(message: Message, command):
    gid = _arg_int(command.args)
    if gid is None:
        return await message.answer("Usage: <code>/preview 12</code>")
    await _do_preview(message, gid, message.from_user.id)


@router.message(Command("schedule", "resume"))
async def cmd_schedule(message: Message, command):
    gid = _arg_int(command.args)
    if gid is None:
        return await message.answer("Usage: <code>/schedule 12</code>")
    ok, note = _set_status(gid, message.from_user.id, QUEUED)
    await message.answer(note)
    if ok:
        await show_view(message, f"g:{gid}:menu", message.from_user.id)


@router.message(Command("pause"))
async def cmd_pause(message: Message, command):
    gid = _arg_int(command.args)
    if gid is None:
        return await message.answer("Usage: <code>/pause 12</code>")
    _, note = _set_status(gid, message.from_user.id, PAUSED)
    await message.answer(note)


@router.message(Command("interval", "cron", "daily", "caption", "target", "tag"))
async def cmd_quick_set(message: Message, command):
    """One-liners for the fields people change most: `/interval 12 30m`."""
    field = {"tag": "tags"}.get(command.command, command.command)
    parts = (command.args or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[0].lstrip("#").isdigit():
        return await message.answer(
            f"Usage: <code>/{command.command} &lt;group id&gt; &lt;value&gt;</code>"
        )
    gid = int(parts[0].lstrip("#"))
    db = SessionLocal()
    try:
        if not _owned(db, gid, message.from_user.id):
            return await message.answer("No such group.")
    finally:
        db.close()

    value = parts[1]
    if field == "caption":
        # Keep whatever formatting the caption carried, minus the command and id.
        value = rich_text(message).split(maxsplit=2)[-1]
    ok, note = apply_group(gid, field, value, message.from_user.id)
    await message.answer(("✅ " if ok else "⚠️ ") + note)
    if ok:
        await show_view(message, f"g:{gid}:menu", message.from_user.id)


@router.message(Command("clone"))
async def cmd_clone(message: Message, command):
    gid = _arg_int(command.args)
    if gid is None:
        return await message.answer("Usage: <code>/clone 12</code>")
    new_id = _clone(gid, message.from_user.id)
    if new_id is None:
        return await message.answer("No such group.")
    await message.answer(f"🧬 Cloned into <code>#{new_id}</code>.")
    await show_view(message, f"g:{new_id}:menu", message.from_user.id)


@router.message(Command("delete"))
async def cmd_delete(message: Message, command):
    gid = _arg_int(command.args)
    if gid is None:
        return await message.answer("Usage: <code>/delete 12</code>")
    await message.answer(
        f"Delete group <code>#{gid}</code> and all its media? This cannot be undone.",
        reply_markup=kb.confirm("delgroup", str(gid), f"g:{gid}:menu"),
    )


@router.message(Command("purge"))
async def cmd_purge(message: Message, command):
    gid = _arg_int(command.args)
    if gid is None:
        return await message.answer("Usage: <code>/purge 12</code>")
    await message.answer(
        f"Remove every media item from group <code>#{gid}</code>?",
        reply_markup=kb.confirm("clearmedia", str(gid), f"g:{gid}:media"),
    )


def _arg_int(args: str | None) -> int | None:
    raw = (args or "").strip().lstrip("#")
    return int(raw) if raw.isdigit() else None


# --------------------------------------------------------------------------
# Media intake
# --------------------------------------------------------------------------

@router.message(F.photo | F.video | F.animation | F.document | F.audio | F.voice
                | F.video_note | F.sticker)
async def on_media(message: Message, state: FSMContext):
    data = await state.get_data()
    gid = data.get("current_group")
    if not gid:
        return await message.answer(
            "No group is selected. Use ➕ <b>New group</b>, <code>/new</code>, or "
            "open a group and press 🖼 <b>Media → Add media</b>.",
            reply_markup=kb.main_menu(),
        )

    media_type, file_obj = None, None
    for kind, getter in MEDIA_KINDS:
        candidate = getter(message)
        if candidate is not None:
            media_type, file_obj = kind, candidate
            break
    if file_obj is None:
        return

    db = SessionLocal()
    try:
        group = _owned(db, gid, message.from_user.id)
        if not group:
            await state.update_data(current_group=None)
            return await message.answer("That group is gone. Pick another one.")

        unique = getattr(file_obj, "file_unique_id", None)
        if unique and db.query(MediaItem).filter(
            MediaItem.group_id == gid, MediaItem.file_unique_id == unique
        ).first():
            return await message.answer("↩️ Already in this group — skipped the duplicate.")

        last = (
            db.query(MediaItem).filter(MediaItem.group_id == gid)
            .order_by(MediaItem.position.desc()).first()
        )
        db.add(MediaItem(
            group_id=gid, file_id=file_obj.file_id, file_unique_id=unique,
            media_type=media_type, position=(last.position or 0) + 1 if last else 1,
        ))
        db.commit()
        total = db.query(MediaItem).filter(MediaItem.group_id == gid).count()
        label = group.label()
    finally:
        db.close()

    # Album uploads arrive as a burst; a reply per file would be a wall of
    # noise, so only the first of a media group gets acknowledged.
    if message.media_group_id and message.media_group_id == data.get("last_album"):
        return
    if message.media_group_id:
        await state.update_data(last_album=message.media_group_id)
    await message.answer(f"➕ Added {media_type} to <b>{esc(label)}</b> — {total} item(s).")


# --------------------------------------------------------------------------
# Group callbacks
# --------------------------------------------------------------------------

@router.callback_query(F.data == "nav:newgroup")
async def cb_new_group(call: CallbackQuery, state: FSMContext):
    gid = create_group(call.from_user.id)
    await state.update_data(current_group=gid)
    await show_view(call, f"g:{gid}:menu", call.from_user.id)
    await call.answer("Group created — send media now.")


@router.callback_query(F.data == "nav:find")
async def cb_find(call: CallbackQuery, state: FSMContext):
    await ask(call, state, "x", 0, "find", "nav:groups")


@router.callback_query(F.data.startswith("g:"))
async def cb_group(call: CallbackQuery, state: FSMContext):
    parts = call.data.split(":")
    gid = int(parts[1])
    action = parts[2]
    extra = parts[3] if len(parts) > 3 else None
    return_view = parts[4] if len(parts) > 4 else None
    user_id = call.from_user.id

    db = SessionLocal()
    try:
        if not _owned(db, gid, user_id):
            return await call.answer("No such group.", show_alert=True)
    finally:
        db.close()

    await state.update_data(current_group=gid)

    if action in {"menu", "media", "targets", "buttons", "sched", "opts"}:
        await show_view(call, f"g:{gid}:{action}", user_id)
        return await call.answer()

    if action == "addmedia":
        await call.message.answer(
            f"🖼 Send photos, videos, GIFs, documents, audio or voice notes now — "
            f"they attach to group <code>#{gid}</code> in the order they arrive."
        )
        return await call.answer()

    if action in {"caption", "rename", "addtarget", "addbtn", "bulkbtn"}:
        field = {"rename": "name", "addtarget": "target"}.get(action, action)
        back = {
            "caption": f"g:{gid}:menu", "rename": f"g:{gid}:menu",
            "addtarget": f"g:{gid}:targets",
            "addbtn": f"g:{gid}:buttons", "bulkbtn": f"g:{gid}:buttons",
        }[action]
        return await ask(call, state, "g", gid, field, back)

    if action == "set" and extra:
        back = f"g:{gid}:sched" if extra in {
            "interval", "cron", "daily", "runat", "jitter", "tz", "quiet",
            "startat", "endat", "maxposts",
        } else f"g:{gid}:opts"
        return await ask(call, state, "g", gid, extra, back)

    if action == "kind" and extra:
        _update(gid, schedule_kind=extra)
        sync_group(gid)
        await show_view(call, f"g:{gid}:sched", user_id)
        return await call.answer(f"Mode: {extra}")

    if action == "opt" and extra in TOGGLES:
        new_value = _toggle(gid, extra)
        # The same toggle appears on several screens, so the button carries the
        # screen to redraw rather than the handler guessing.
        await show_view(call, f"g:{gid}:{return_view or 'opts'}", user_id)
        return await call.answer(f"{extra.replace('_', ' ')}: {'on' if new_value else 'off'}")

    if action == "cyclemode":
        db = SessionLocal()
        try:
            group = db.get(ContentGroup, gid)
            current = group.caption_mode or "HTML"
            group.caption_mode = MODE_CYCLE[(MODE_CYCLE.index(current) + 1) % len(MODE_CYCLE)]
            new_mode = group.caption_mode
            db.commit()
        finally:
            db.close()
        await show_view(call, f"g:{gid}:opts", user_id)
        return await call.answer(f"Parse mode: {new_mode}")

    if action == "activate":
        ok, note = _set_status(gid, user_id, QUEUED)
        await show_view(call, f"g:{gid}:menu", user_id)
        return await call.answer(note, show_alert=not ok)

    if action == "pause":
        _, note = _set_status(gid, user_id, PAUSED)
        await show_view(call, f"g:{gid}:menu", user_id)
        return await call.answer(note)

    if action == "postnow":
        await call.answer("Posting…")
        return await _do_post(call.message, gid, user_id, refresh=call)

    if action == "preview":
        await call.answer("Sending preview…")
        return await _do_preview(call.message, gid, user_id)

    if action == "stats":
        await swap(call, _group_stats(gid, user_id), kb.simple_back(f"g:{gid}:menu"))
        return await call.answer()

    if action == "clone":
        new_id = _clone(gid, user_id)
        await show_view(call, f"g:{new_id}:menu", user_id)
        return await call.answer("Cloned.")

    if action == "reverse":
        _reverse_media(gid)
        await show_view(call, f"g:{gid}:media", user_id)
        return await call.answer("Order reversed.")

    if action == "resetcount":
        _update(gid, posts_sent=0)
        await show_view(call, f"g:{gid}:sched", user_id)
        return await call.answer("Counter reset.")

    if action == "clearbtn":
        _update(gid, buttons_json=None)
        await show_view(call, f"g:{gid}:buttons", user_id)
        return await call.answer("Buttons cleared.")

    if action == "delbtn" and extra:
        row_index, col_index = (int(x) for x in extra.split("_"))
        _delete_button(gid, row_index, col_index)
        await show_view(call, f"g:{gid}:buttons", user_id)
        return await call.answer("Removed.")

    if action in {"delete", "clearmedia"}:
        back = f"g:{gid}:menu" if action == "delete" else f"g:{gid}:media"
        question = (
            "Delete this group, its media and its schedule? This cannot be undone."
            if action == "delete" else
            "Remove every media item from this group?"
        )
        await swap(call, question, kb.confirm(action if action == "clearmedia" else "delgroup", str(gid), back))
        return await call.answer()

    await call.answer()


@router.callback_query(F.data.startswith("ok:delgroup:") | F.data.startswith("ok:clearmedia:"))
async def cb_confirm_group(call: CallbackQuery, state: FSMContext):
    _, action, raw = call.data.split(":", 2)
    gid = int(raw)
    user_id = call.from_user.id

    db = SessionLocal()
    try:
        group = _owned(db, gid, user_id)
        if not group:
            return await call.answer("Already gone.", show_alert=True)
        db.query(MediaItem).filter(MediaItem.group_id == gid).delete()
        if action == "delgroup":
            db.query(Target).filter(Target.group_id == gid).delete()
            db.query(QueueItem).filter(QueueItem.group_id == gid).delete()
            db.query(PostLog).filter(PostLog.group_id == gid).delete()
            db.delete(group)
        db.commit()
    finally:
        db.close()

    if action == "delgroup":
        sync_group(gid)  # the row is gone, so this just retires its jobs
        await state.update_data(current_group=None)
        await show_view(call, "nav:groups", user_id)
        return await call.answer("Deleted.")

    await show_view(call, f"g:{gid}:media", user_id)
    await call.answer("Media cleared.")


# --------------------------------------------------------------------------
# Media item and target callbacks
# --------------------------------------------------------------------------

@router.callback_query(F.data.startswith("m:"))
async def cb_media_item(call: CallbackQuery):
    _, raw, action = call.data.split(":")
    item_id = int(raw)

    db = SessionLocal()
    try:
        item = db.get(MediaItem, item_id)
        if not item:
            return await call.answer("Item already removed.")
        gid, file_id, media_type = item.group_id, item.file_id, item.media_type
        siblings = (
            db.query(MediaItem).filter(MediaItem.group_id == gid)
            .order_by(MediaItem.position, MediaItem.id).all()
        )
        index = next(i for i, m in enumerate(siblings) if m.id == item_id)

        if action == "del":
            db.delete(item)
        elif action in {"up", "down"}:
            neighbour = index - 1 if action == "up" else index + 1
            if 0 <= neighbour < len(siblings):
                # Rewrite the whole run so items that were never explicitly
                # ordered (all position 0) still end up with distinct ranks.
                reordered = list(siblings)
                reordered[index], reordered[neighbour] = reordered[neighbour], reordered[index]
                for rank, row in enumerate(reordered, start=1):
                    row.position = rank
        db.commit()
    finally:
        db.close()

    if action == "show":
        await _preview_item(call, media_type, file_id)
        return await call.answer()

    text, markup = views.media_view(gid, 0)
    await swap(call, text, markup)
    await call.answer("Removed." if action == "del" else "Moved.")


async def _preview_item(call: CallbackQuery, media_type: str, file_id: str):
    senders = {
        "photo": call.message.answer_photo,
        "video": call.message.answer_video,
        "animation": call.message.answer_animation,
        "document": call.message.answer_document,
        "audio": call.message.answer_audio,
        "voice": call.message.answer_voice,
        "video_note": call.message.answer_video_note,
        "sticker": call.message.answer_sticker,
    }
    send = senders.get(media_type)
    try:
        if send:
            return await send(file_id)
    except Exception as exc:
        log.info("Could not echo %s: %s", media_type, exc)
    await call.message.answer(f"<code>{esc(media_type)}</code> — <code>{esc(file_id)}</code>")


@router.callback_query(F.data.startswith("t:"))
async def cb_target(call: CallbackQuery):
    _, raw, action = call.data.split(":")
    target_id = int(raw)

    db = SessionLocal()
    try:
        target = db.get(Target, target_id)
        if not target:
            return await call.answer("Target already removed.")
        gid = target.group_id
        if action == "toggle":
            target.enabled = not target.enabled
            note = "Enabled." if target.enabled else "Disabled."
        else:
            db.delete(target)
            note = "Removed."
        db.commit()
    finally:
        db.close()

    text, markup = views.targets_view(gid)
    await swap(call, text, markup)
    await call.answer(note)


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------

async def _do_post(message: Message, gid: int, user_id: int, refresh: CallbackQuery | None = None):
    ok, errors, _ = await post_group(gid, trigger="manual")
    if ok:
        note = f"📤 Posted to {ok} chat(s)."
    else:
        note = "⚠️ Nothing was posted."
    if errors:
        note += "\n\n" + "\n".join(f"• <code>{esc(truncate(e, 150))}</code>" for e in errors[:4])
    await message.answer(note)
    if refresh:
        await show_view(refresh, f"g:{gid}:menu", user_id)


async def _do_preview(message: Message, gid: int, user_id: int):
    """Deliver to the admin's own chat without touching counters or status."""
    ok, errors, _ = await post_group(gid, trigger="preview",
                                     override_chat=str(message.chat.id), count_it=False)
    if not ok:
        await message.answer(
            "⚠️ Preview failed:\n" + "\n".join(f"<code>{esc(truncate(e, 150))}</code>" for e in errors[:3])
        )
    else:
        await message.answer("👆 That is exactly what subscribers will receive.")


def _group_stats(gid: int, user_id: int) -> str:
    tz = user_tz(user_id)
    db = SessionLocal()
    try:
        group = db.get(ContentGroup, gid)
        logs = db.query(PostLog).filter(PostLog.group_id == gid).all()
        media = db.query(MediaItem).filter(MediaItem.group_id == gid).count()
        queues = (
            db.query(Queue.name, Queue.id)
            .join(QueueItem, QueueItem.queue_id == Queue.id)
            .filter(QueueItem.group_id == gid).all()
        )
        label = group.label()
        posts_sent, next_run, last_run = group.posts_sent or 0, group.next_run_at, group.last_run_at
    finally:
        db.close()

    ok = sum(1 for entry in logs if entry.ok)
    return (
        f"📊 <b>{esc(label)}</b>\n\n"
        f"Media items: {media}\n"
        f"Runs counted: {posts_sent}\n"
        f"Deliveries: {ok} ok · {len(logs) - ok} failed\n"
        f"Next run: {esc(format_dt(next_run, tz))}\n"
        f"Last run: {esc(format_dt(last_run, tz))}\n"
        + (f"In queues: {esc(', '.join(name or f'#{qid}' for name, qid in queues))}" if queues else "")
    )


def _update(group_id: int, **fields):
    db = SessionLocal()
    try:
        group = db.get(ContentGroup, group_id)
        if not group:
            return
        for key, value in fields.items():
            setattr(group, key, value)
        db.commit()
    finally:
        db.close()


def _toggle(group_id: int, field: str) -> bool:
    db = SessionLocal()
    try:
        group = db.get(ContentGroup, group_id)
        if not group:
            return False
        new_value = not bool(getattr(group, field))
        setattr(group, field, new_value)
        db.commit()
        return new_value
    finally:
        db.close()


def _set_status(group_id: int, user_id: int, status: str) -> tuple[bool, str]:
    """Activate or pause, refusing to activate something that cannot post."""
    db = SessionLocal()
    try:
        group = _owned(db, group_id, user_id)
        if not group:
            return False, "No such group."
        if status == QUEUED:
            has_target = (
                db.query(Target).filter(Target.group_id == group_id, Target.enabled.is_(True)).count()
                or group.target_chat_id
            )
            has_content = (
                db.query(MediaItem).filter(MediaItem.group_id == group_id).count() or group.caption
            )
            if not has_target:
                return False, "Add a target chat first."
            if not has_content:
                return False, "Add media or a caption first."
            if not build_triggers(group):
                return False, "Schedule is incomplete — set an interval, cron or daily time."
        group.status = status
        db.commit()
    finally:
        db.close()

    next_run = sync_group(group_id)
    if status != QUEUED:
        return True, "⏸ Paused."
    if not next_run:
        # Registered, but the scheduler has not computed a fire time yet.
        return True, "▶️ Active."
    return True, f"▶️ Active. Next run in {format_duration(int(next_run.timestamp() - time.time()))}."


def _clone(group_id: int, user_id: int) -> int | None:
    db = SessionLocal()
    try:
        source = _owned(db, group_id, user_id)
        if not source:
            return None
        clone = ContentGroup(
            owner_id=user_id,
            name=f"{source.label()} (copy)"[:64],
            tags=source.tags,
            caption=source.caption,
            caption_mode=source.caption_mode,
            buttons_json=source.buttons_json,
            status=DRAFT,
            schedule_kind=source.schedule_kind,
            interval_seconds=source.interval_seconds,
            cron_expr=source.cron_expr,
            daily_times=source.daily_times,
            jitter_seconds=source.jitter_seconds,
            timezone_name=source.timezone_name,
            quiet_start=source.quiet_start,
            quiet_end=source.quiet_end,
            max_posts=source.max_posts,
            shuffle=source.shuffle,
            rotate_targets=source.rotate_targets,
            buttons_attach=source.buttons_attach,
            silent=source.silent,
            protect_content=source.protect_content,
            pin_post=source.pin_post,
            delete_after=source.delete_after,
            notify_owner=source.notify_owner,
        )
        db.add(clone)
        db.commit()
        new_id = clone.id
        for item in db.query(MediaItem).filter(MediaItem.group_id == group_id).order_by(MediaItem.position).all():
            db.add(MediaItem(
                group_id=new_id, file_id=item.file_id, file_unique_id=item.file_unique_id,
                media_type=item.media_type, position=item.position,
            ))
        for target in db.query(Target).filter(Target.group_id == group_id).all():
            db.add(Target(group_id=new_id, chat_id=target.chat_id, title=target.title,
                          thread_id=target.thread_id, enabled=target.enabled))
        db.commit()
    finally:
        db.close()
    return new_id


def _reverse_media(group_id: int):
    db = SessionLocal()
    try:
        items = (
            db.query(MediaItem).filter(MediaItem.group_id == group_id)
            .order_by(MediaItem.position, MediaItem.id).all()
        )
        for position, item in enumerate(reversed(items), start=1):
            item.position = position
        db.commit()
    finally:
        db.close()


def _delete_button(group_id: int, row_index: int, col_index: int):
    db = SessionLocal()
    try:
        group = db.get(ContentGroup, group_id)
        if not group:
            return
        rows = group.buttons()
        if 0 <= row_index < len(rows) and 0 <= col_index < len(rows[row_index]):
            rows[row_index].pop(col_index)
            rows = [r for r in rows if r]
        group.buttons_json = json.dumps(rows) if rows else None
        db.commit()
    finally:
        db.close()
