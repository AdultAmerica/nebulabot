"""Screen rendering: each function returns the (text, keyboard) for one panel.

Handlers stay thin because every screen can be rebuilt from an id alone, which
is also what makes "refresh" and "go back" work from any depth.
"""

from sqlalchemy import func

import keyboards as kb
from access import get_prefs, list_admins, user_tz
from db import (
    DONE, DRAFT, KIND_CRON, KIND_DAILY, KIND_INTERVAL, KIND_ONCE, PAUSED,
    POSTED, QUEUED, ContentGroup, MediaItem, PostLog, Queue, QueueItem,
    SessionLocal, Target,
)
from scheduling import describe_schedule, scheduler
from utils import esc, format_dt, format_duration, format_hhmm, truncate

PAGE = kb.PAGE_SIZE

STATUS_TEXT = {
    DRAFT: "📝 draft", QUEUED: "🟢 active", PAUSED: "⏸ paused",
    POSTED: "📤 posted", DONE: "🏁 finished",
}


def _media_count(group_id: int) -> int:
    db = SessionLocal()
    try:
        return db.query(func.count(MediaItem.id)).filter(
            MediaItem.group_id == group_id
        ).scalar() or 0
    finally:
        db.close()


def _counts(db, group_id: int) -> tuple[int, int]:
    media = db.query(func.count(MediaItem.id)).filter(MediaItem.group_id == group_id).scalar() or 0
    targets = db.query(func.count(Target.id)).filter(Target.group_id == group_id).scalar() or 0
    return media, targets


# --------------------------------------------------------------------------
# Groups
# --------------------------------------------------------------------------

def group_list_view(user_id: int, page: int = 0, query: str | None = None):
    db = SessionLocal()
    try:
        base = db.query(ContentGroup).filter(ContentGroup.owner_id == user_id)
        if query:
            like = f"%{query}%"
            base = base.filter(
                ContentGroup.name.ilike(like)
                | ContentGroup.tags.ilike(like)
                | ContentGroup.caption.ilike(like)
            )
        total = base.count()
        rows = base.order_by(ContentGroup.id.desc()).offset(page * PAGE).limit(PAGE).all()
        listing = []
        for g in rows:
            media, _ = _counts(db, g.id)
            listing.append((g.id, g.label(), g.status, media))
    finally:
        db.close()

    if not total:
        text = (
            "📂 <b>No groups yet</b>\n\n"
            "A <i>group</i> is one post: its media, caption, buttons, targets and "
            "schedule. Create one, drop photos or videos into it, point it at a "
            "channel, and give it an interval."
        )
    else:
        header = f"📂 <b>Groups</b> — {total} total"
        if query:
            header += f" · matching <code>{esc(query)}</code>"
        text = header + "\n\n" + "\n".join(
            f"{kb.STATUS_ICON.get(status, '•')} <b>#{gid}</b> {esc(truncate(label, 30))} — {media} item(s)"
            for gid, label, status, media in listing
        )
    return text, kb.group_list(listing, page, total)


def group_view(group_id: int, user_id: int):
    tz = user_tz(user_id)
    db = SessionLocal()
    try:
        group = db.get(ContentGroup, group_id)
        if not group:
            return "That group no longer exists.", kb.main_menu()
        media, targets = _counts(db, group_id)
        target_names = [
            t.label() for t in db.query(Target)
            .filter(Target.group_id == group_id, Target.enabled.is_(True)).limit(4).all()
        ]
        db.expunge(group)
    finally:
        db.close()

    lines = [
        f"<b>{esc(group.label())}</b>  <code>#{group.id}</code>",
        f"Status: {STATUS_TEXT.get(group.status, group.status)}",
        f"Media: {media} · Buttons: {sum(len(r) for r in group.buttons())} · Targets: {targets}",
        f"Schedule: {esc(describe_schedule(group))}",
        f"Next run: {esc(format_dt(group.next_run_at, tz))}",
        f"Last run: {esc(format_dt(group.last_run_at, tz))}",
        f"Posts sent: {group.posts_sent or 0}" + (f" / {group.max_posts}" if group.max_posts else ""),
    ]
    if target_names:
        lines.append("To: " + esc(", ".join(target_names)))
    if group.tag_list():
        lines.append("Tags: " + esc(", ".join(group.tag_list())))
    if group.caption:
        lines.append(f"\nCaption preview:\n<i>{esc(truncate(group.caption, 160))}</i>")
    if group.last_error:
        lines.append(f"\n⚠️ Last error: <code>{esc(truncate(group.last_error, 200))}</code>")

    return "\n".join(lines), kb.group_menu(group, media, targets)


def media_view(group_id: int, page: int = 0):
    db = SessionLocal()
    try:
        group = db.get(ContentGroup, group_id)
        if not group:
            return "That group no longer exists.", kb.main_menu()
        base = db.query(MediaItem).filter(MediaItem.group_id == group_id)
        total = base.count()
        rows = base.order_by(MediaItem.position, MediaItem.id).offset(page * PAGE).limit(PAGE).all()
        items = [(m.id, m.media_type, m.position or 0) for m in rows]
        db.expunge(group)
    finally:
        db.close()

    text = (
        f"🖼 <b>Media — {esc(group.label())}</b>\n\n"
        f"{total} item(s). Albums are cut into batches of 10 automatically, and "
        f"the caption rides on the first message.\n"
        f"Send photos, videos, GIFs, documents, audio or voice notes to add more."
    )
    if group.shuffle:
        text += "\n\n🔀 Shuffle is on — the order below is reshuffled on every post."
    return text, kb.media_menu(group, items, page, total)


def targets_view(group_id: int):
    db = SessionLocal()
    try:
        group = db.get(ContentGroup, group_id)
        if not group:
            return "That group no longer exists.", kb.main_menu()
        targets = [
            (t.id, t.label(), t.enabled)
            for t in db.query(Target).filter(Target.group_id == group_id).order_by(Target.id).all()
        ]
        db.expunge(group)
    finally:
        db.close()

    text = (
        f"🎯 <b>Targets — {esc(group.label())}</b>\n\n"
        "Every enabled target receives the post. Add the bot to the channel as "
        "an admin first, then send the numeric ID (<code>-1001234567890</code>) "
        "or the @username.\n\n"
        "With <b>rotate</b> on, each run goes to one target in turn instead of "
        "all of them — useful for spreading the same album across channels."
    )
    if not targets:
        text += "\n\n⚠️ No targets yet, so nothing can post."
    return text, kb.targets_menu(group, targets)


def buttons_view(group_id: int):
    db = SessionLocal()
    try:
        group = db.get(ContentGroup, group_id)
        if not group:
            return "That group no longer exists.", kb.main_menu()
        db.expunge(group)
    finally:
        db.close()

    rows = group.buttons()
    count = sum(len(r) for r in rows)
    media = _media_count(group.id)

    if media <= 1:
        placement = (
            "✅ Attached directly to the post — one message, buttons included."
        )
    elif group.buttons_attach is not False:
        placement = (
            f"✅ Attached to the post. Telegram allows no keyboard on an album, "
            f"so the first item is sent on its own carrying the caption and "
            f"buttons, and the other {media - 1} follow as an album beneath.\n\n"
            f"For a <b>single</b> post with buttons, keep just one photo or "
            f"video in the group."
        )
    else:
        placement = (
            "⚠️ Sent underneath in their own message, with the caption, because "
            "<b>Buttons on media</b> is off and Telegram allows no keyboard on "
            "an album."
        )

    text = (
        f"🔘 <b>Buttons — {esc(group.label())}</b>\n\n"
        f"{count} button(s) in {len(rows)} row(s).\n\n{placement}"
    )
    return text, kb.buttons_menu(group)


def schedule_view(group_id: int, user_id: int):
    tz = user_tz(user_id)
    db = SessionLocal()
    try:
        group = db.get(ContentGroup, group_id)
        if not group:
            return "That group no longer exists.", kb.main_menu()
        db.expunge(group)
    finally:
        db.close()

    hints = {
        KIND_INTERVAL: "Repeats forever at a fixed spacing — <code>30m</code>, <code>2h</code>, <code>1d 6h</code>.",
        KIND_CRON: "Five cron fields: <code>minute hour day month weekday</code>. "
                   "<code>0 */4 * * *</code> is every four hours on the hour.",
        KIND_DAILY: "One or more clock times a day: <code>09:00, 18:30</code>.",
        KIND_ONCE: "Fires a single time, then the group retires itself.",
    }
    lines = [
        f"⏱ <b>Schedule — {esc(group.label())}</b>",
        "",
        f"Mode: {esc(describe_schedule(group))}",
        hints.get(group.schedule_kind or KIND_INTERVAL, ""),
        "",
        f"Timezone: <code>{esc(group.timezone_name or 'UTC')}</code>",
        f"Jitter: {format_duration(group.jitter_seconds)} "
        "<i>(random spread so posts never land on the exact minute)</i>",
        f"Quiet hours: {format_hhmm(group.quiet_start)}–{format_hhmm(group.quiet_end)} "
        "<i>(runs inside this window are skipped)</i>",
        f"Window: {esc(format_dt(group.start_at, tz, False))} → {esc(format_dt(group.end_at, tz, False))}",
        f"Posts: {group.posts_sent or 0}" + (f" of {group.max_posts} max" if group.max_posts else ""),
        f"Next run: {esc(format_dt(group.next_run_at, tz))}",
    ]
    return "\n".join(line for line in lines if line is not None), kb.schedule_menu(group)


def options_view(group_id: int):
    db = SessionLocal()
    try:
        group = db.get(ContentGroup, group_id)
        if not group:
            return "That group no longer exists.", kb.main_menu()
        db.expunge(group)
    finally:
        db.close()

    text = (
        f"🎛 <b>Options — {esc(group.label())}</b>\n\n"
        "🔕 <b>Silent</b> — deliver without a notification sound.\n"
        "🛡 <b>Protect</b> — block forwarding and saving.\n"
        "📌 <b>Pin</b> — pin the first message of each post.\n"
        "🔀 <b>Shuffle</b> — reorder media on every run.\n"
        "🔘 <b>Buttons on media</b> — keep the keyboard attached to a media "
        "post instead of a message below it.\n"
        "🔁 <b>Rotate targets</b> — one target per run instead of all.\n"
        "🔔 <b>Alerts</b> — DM you when a scheduled post fails.\n"
        "🧹 <b>Auto-delete</b> — remove the post after a delay.\n"
        "🅰️ <b>Parse mode</b> — how the caption is formatted."
    )
    return text, kb.options_menu(group)


# --------------------------------------------------------------------------
# Queues
# --------------------------------------------------------------------------

def queue_list_view(user_id: int, page: int = 0):
    db = SessionLocal()
    try:
        base = db.query(Queue).filter(Queue.owner_id == user_id)
        total = base.count()
        rows = base.order_by(Queue.id.desc()).offset(page * PAGE).limit(PAGE).all()
        listing = []
        for q in rows:
            count = db.query(func.count(QueueItem.id)).filter(QueueItem.queue_id == q.id).scalar() or 0
            listing.append((q.id, q.label(), q.status, count))
    finally:
        db.close()

    text = (
        "🔁 <b>Queues</b>\n\n"
        "A group's own schedule reposts the <i>same</i> album forever. A queue "
        "walks a <i>list</i> of groups: every tick posts the next one and moves "
        "the cursor along — a drip campaign rather than a repeating ad.\n\n"
        "Set the interval once on the queue; the member groups do not need "
        "schedules of their own."
    )
    if listing:
        text += "\n\n" + "\n".join(
            f"{kb.STATUS_ICON.get(status, '•')} <b>#{qid}</b> {esc(truncate(label, 30))} — {count} group(s)"
            for qid, label, status, count in listing
        )
    return text, kb.queue_list(listing, page, total)


def queue_view(queue_id: int, user_id: int):
    tz = user_tz(user_id)
    db = SessionLocal()
    try:
        queue = db.get(Queue, queue_id)
        if not queue:
            return "That queue no longer exists.", kb.main_menu()
        count = db.query(func.count(QueueItem.id)).filter(QueueItem.queue_id == queue_id).scalar() or 0
        db.expunge(queue)
    finally:
        db.close()

    lines = [
        f"🔁 <b>{esc(queue.label())}</b>  <code>#{queue.id}</code>",
        f"Status: {STATUS_TEXT.get(queue.status, queue.status)}",
        f"Groups in rota: {count} · cursor at {(queue.cursor or 0) + 1 if count else 0}",
        f"Schedule: {esc(describe_schedule(queue))}",
        f"Next run: {esc(format_dt(queue.next_run_at, tz))}",
        f"Last run: {esc(format_dt(queue.last_run_at, tz))}",
        f"Posts sent: {queue.posts_sent or 0}",
    ]
    if queue.last_error:
        lines.append(f"\n⚠️ Last error: <code>{esc(truncate(queue.last_error, 200))}</code>")
    if not count:
        lines.append("\n⚠️ Add at least one group before activating.")
    return "\n".join(lines), kb.queue_menu(queue, count)


def queue_items_view(queue_id: int):
    db = SessionLocal()
    try:
        queue = db.get(Queue, queue_id)
        if not queue:
            return "That queue no longer exists.", kb.main_menu()
        rows = (
            db.query(QueueItem, ContentGroup)
            .outerjoin(ContentGroup, ContentGroup.id == QueueItem.group_id)
            .filter(QueueItem.queue_id == queue_id)
            .order_by(QueueItem.position, QueueItem.id)
            .all()
        )
        items = [
            (qi.id, qi.group_id, g.label() if g else f"#{qi.group_id} (deleted)")
            for qi, g in rows
        ]
        cursor = queue.cursor or 0
        label = queue.label()
    finally:
        db.close()

    text = f"📋 <b>Rota — {esc(label)}</b>\n\n"
    if items:
        text += "\n".join(
            f"{'▶️' if index == cursor else '  '} {index + 1}. {esc(truncate(name, 34))}"
            for index, (_, _, name) in enumerate(items)
        )
        text += "\n\n▶️ marks the group that posts next."
    else:
        text += "Empty. Add groups to build the rota."
    return text, kb.queue_items_menu(queue_id, items)


# --------------------------------------------------------------------------
# Settings, stats, tools
# --------------------------------------------------------------------------

def settings_view(user_id: int):
    prefs = get_prefs(user_id)
    text = (
        "⚙️ <b>Settings</b>\n\n"
        "These apply to everything you create from now on. Existing groups keep "
        "whatever they were given.\n\n"
        f"Your timezone drives every clock time you type — quiet hours, daily "
        f"times and one-shot dates are all read in <code>{esc(prefs.timezone_name or 'UTC')}</code>."
    )
    return text, kb.settings_menu(prefs)


def stats_view(user_id: int):
    tz = user_tz(user_id)
    db = SessionLocal()
    try:
        groups = db.query(ContentGroup).filter(ContentGroup.owner_id == user_id).all()
        group_ids = [g.id for g in groups]
        media = (
            db.query(func.count(MediaItem.id))
            .filter(MediaItem.group_id.in_(group_ids)).scalar() or 0
        ) if group_ids else 0
        logs = db.query(PostLog).filter(PostLog.group_id.in_(group_ids)).all() if group_ids else []
        queues = db.query(Queue).filter(Queue.owner_id == user_id).all()
        targets = (
            db.query(func.count(func.distinct(Target.chat_id)))
            .filter(Target.group_id.in_(group_ids)).scalar() or 0
        ) if group_ids else 0
        recent = (
            db.query(PostLog)
            .filter(PostLog.group_id.in_(group_ids))
            .order_by(PostLog.id.desc()).limit(1).all()
        ) if group_ids else []
        last_post = recent[0].created_at if recent else None
    finally:
        db.close()

    by_status: dict[str, int] = {}
    for g in groups:
        by_status[g.status] = by_status.get(g.status, 0) + 1
    ok = sum(1 for entry in logs if entry.ok)
    failed = len(logs) - ok
    jobs = len(scheduler.get_jobs())

    text = (
        "📊 <b>Statistics</b>\n\n"
        f"Groups: <b>{len(groups)}</b> "
        + (" · ".join(f"{STATUS_TEXT.get(s, s)}: {n}" for s, n in sorted(by_status.items())) or "—")
        + f"\nQueues: <b>{len(queues)}</b>\n"
        f"Media items: <b>{media}</b>\n"
        f"Distinct target chats: <b>{targets}</b>\n\n"
        f"Deliveries: <b>{ok}</b> ok · <b>{failed}</b> failed\n"
        f"Last delivery: {esc(format_dt(last_post, tz))}\n"
        f"Scheduler jobs live: <b>{jobs}</b>"
    )
    return text, kb.tools_menu()


def history_view(user_id: int, limit: int = 15):
    tz = user_tz(user_id)
    db = SessionLocal()
    try:
        group_ids = [g.id for g in db.query(ContentGroup.id).filter(ContentGroup.owner_id == user_id).all()]
        rows = (
            db.query(PostLog)
            .filter(PostLog.group_id.in_(group_ids))
            .order_by(PostLog.id.desc()).limit(limit).all()
        ) if group_ids else []
        entries = [
            (r.created_at, r.group_id, r.chat_id, r.ok, r.error, r.trigger, r.item_count)
            for r in rows
        ]
    finally:
        db.close()

    if not entries:
        return "🧾 <b>History</b>\n\nNothing has been posted yet.", kb.tools_menu()

    lines = ["🧾 <b>Recent deliveries</b>", ""]
    for created, gid, chat, ok, error, trigger, count in entries:
        mark = "✅" if ok else "❌"
        line = f"{mark} <code>{esc(format_dt(created, tz, False))}</code> #{gid} → {esc(chat)} ({trigger}, {count})"
        if not ok and error:
            line += f"\n   <i>{esc(truncate(error, 90))}</i>"
        lines.append(line)
    return "\n".join(lines), kb.tools_menu()


def jobs_view():
    from scheduling import job_report
    lines = job_report()
    if not lines:
        text = "📜 <b>Scheduler</b>\n\nNo jobs registered. Activate a group or queue."
    else:
        text = "📜 <b>Scheduler jobs</b>\n\n<code>" + esc("\n".join(lines[:40])) + "</code>"
    return text, kb.tools_menu()


def admins_view():
    admins = list_admins()
    text = (
        "🔑 <b>Admins</b>\n\n"
        "👑 marks bootstrap admins from <code>ADMIN_IDS</code> in the environment "
        "— they cannot be removed from chat, so you can always recover access.\n\n"
        "Admins share nothing: each one only sees the groups and queues they own."
    )
    return text, kb.admins_menu(admins)
