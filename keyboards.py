"""Every inline keyboard the panel shows.

Callback data is `<scope>:<id>:<action>[:<extra>]`, which keeps the routing
filters in the handlers to a single `startswith` each and stays well inside
Telegram's 64-byte budget.
"""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from db import (
    DONE, DRAFT, KIND_CRON, KIND_DAILY, KIND_INTERVAL, KIND_ONCE, PAUSED,
    QUEUED,
)
from utils import format_duration, format_hhmm, truncate

PAGE_SIZE = 8


def _rows(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[r for r in rows if r])


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _flag(value) -> str:
    return "✅" if value else "❌"


def _button_layout(group) -> str:
    """Neither state is 'off', so name the layout rather than tick a box."""
    return "on first item" if group.buttons_attach else "below album"


# --------------------------------------------------------------------------
# Top level
# --------------------------------------------------------------------------

def main_menu() -> InlineKeyboardMarkup:
    return _rows(
        [btn("➕ New group", "nav:newgroup"), btn("📂 Groups", "nav:groups")],
        [btn("🔁 Queues", "nav:queues"), btn("📊 Stats", "nav:stats")],
        [btn("🛠 Tools", "nav:tools"), btn("⚙️ Settings", "nav:settings")],
        [btn("❓ Help", "help:index")],
    )


def back_to(data: str, label: str = "⬅️ Back") -> list:
    return [btn(label, data)]


def paginator(scope: str, page: int, total: int) -> list:
    """Prev/next row, omitted entirely when everything fits on one page."""
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    if pages == 1:
        return []
    row = []
    if page > 0:
        row.append(btn("◀️", f"page:{scope}:{page - 1}"))
    row.append(btn(f"{page + 1}/{pages}", "noop"))
    if page < pages - 1:
        row.append(btn("▶️", f"page:{scope}:{page + 1}"))
    return row


# --------------------------------------------------------------------------
# Groups
# --------------------------------------------------------------------------

STATUS_ICON = {DRAFT: "📝", QUEUED: "🟢", PAUSED: "⏸", "posted": "📤", DONE: "🏁"}


def group_list(groups, page: int, total: int) -> InlineKeyboardMarkup:
    rows = [
        [btn(f"{STATUS_ICON.get(status, '•')} {truncate(label, 28)} · {count}🖼",
             f"g:{gid}:menu")]
        for gid, label, status, count in groups
    ]
    return _rows(
        *rows,
        paginator("groups", page, total),
        [btn("➕ New", "nav:newgroup"), btn("🔎 Find", "nav:find")],
        back_to("nav:main"),
    )


def group_menu(group, media_count: int, target_count: int) -> InlineKeyboardMarkup:
    gid = group.id
    running = group.status == QUEUED
    return _rows(
        [btn(f"🖼 Media ({media_count})", f"g:{gid}:media"),
         btn("✏️ Caption", f"g:{gid}:caption")],
        [btn(f"🔘 Buttons ({len(group.buttons())})", f"g:{gid}:buttons"),
         btn(f"🎯 Targets ({target_count})", f"g:{gid}:targets")],
        [btn("⏱ Schedule", f"g:{gid}:sched"), btn("🎛 Options", f"g:{gid}:opts")],
        [btn("👁 Preview", f"g:{gid}:preview"), btn("📤 Post now", f"g:{gid}:postnow")],
        [btn("⏸ Pause" if running else "▶️ Activate",
             f"g:{gid}:{'pause' if running else 'activate'}"),
         btn("📊 Stats", f"g:{gid}:stats")],
        [btn("🏷 Rename", f"g:{gid}:rename"), btn("🧬 Clone", f"g:{gid}:clone")],
        [btn("🗑 Delete", f"g:{gid}:delete"), btn("🔄 Refresh", f"g:{gid}:menu")],
        back_to("nav:groups"),
    )


def media_menu(group, items, page: int, total: int) -> InlineKeyboardMarkup:
    gid = group.id
    rows = []
    for index, (item_id, media_type, position) in enumerate(items):
        absolute = page * PAGE_SIZE + index
        rows.append([
            btn(f"{absolute + 1}. {media_type}", f"m:{item_id}:show"),
            btn("⬆️", f"m:{item_id}:up"),
            btn("⬇️", f"m:{item_id}:down"),
            btn("❌", f"m:{item_id}:del"),
        ])
    return _rows(
        *rows,
        paginator(f"media_{gid}", page, total),
        [btn("➕ Add media", f"g:{gid}:addmedia"),
         btn(f"🔀 Shuffle {_flag(group.shuffle)}", f"g:{gid}:opt:shuffle:media")],
        [btn("🔁 Reverse", f"g:{gid}:reverse"), btn("🧹 Clear all", f"g:{gid}:clearmedia")],
        back_to(f"g:{gid}:menu"),
    )


def targets_menu(group, targets) -> InlineKeyboardMarkup:
    gid = group.id
    rows = [
        [btn(f"{_flag(enabled)} {truncate(label, 26)}", f"t:{tid}:toggle"),
         btn("❌", f"t:{tid}:del")]
        for tid, label, enabled in targets
    ]
    return _rows(
        *rows,
        [btn("➕ Add target", f"g:{gid}:addtarget")],
        [btn(f"🔀 Rotate targets {_flag(group.rotate_targets)}", f"g:{gid}:opt:rotate_targets:targets")],
        back_to(f"g:{gid}:menu"),
    )


def buttons_menu(group) -> InlineKeyboardMarkup:
    gid = group.id
    rows = []
    for row_index, row in enumerate(group.buttons()):
        for col_index, button in enumerate(row):
            rows.append([
                btn(f"R{row_index + 1} · {truncate(button.get('text', '?'), 24)}", "noop"),
                btn("❌", f"g:{gid}:delbtn:{row_index}_{col_index}"),
            ])
    return _rows(
        *rows,
        [btn("➕ Add button", f"g:{gid}:addbtn"), btn("🧩 Bulk edit", f"g:{gid}:bulkbtn")],
        [btn(f"📮 One item per post {_flag(group.one_per_post)}",
             f"g:{gid}:opt:one_per_post:buttons")],
        [btn(f"🔘 Buttons: {_button_layout(group)}", f"g:{gid}:opt:buttons_attach:buttons")],
        [btn("🧹 Clear", f"g:{gid}:clearbtn")],
        back_to(f"g:{gid}:menu"),
    )


KIND_LABEL = {
    KIND_INTERVAL: "⏱ Interval", KIND_CRON: "🗓 Cron",
    KIND_DAILY: "🌅 Daily", KIND_ONCE: "📌 Once",
}


def schedule_menu(group) -> InlineKeyboardMarkup:
    gid = group.id
    kind = group.schedule_kind or KIND_INTERVAL
    kind_row = [
        btn(("• " if kind == k else "") + label, f"g:{gid}:kind:{k}")
        for k, label in KIND_LABEL.items()
    ]
    value_button = {
        KIND_INTERVAL: btn(f"Every {format_duration(group.interval_seconds)}", f"g:{gid}:set:interval"),
        KIND_CRON: btn(f"Cron: {group.cron_expr or '—'}", f"g:{gid}:set:cron"),
        KIND_DAILY: btn(f"At: {group.daily_times or '—'}", f"g:{gid}:set:daily"),
        KIND_ONCE: btn("Set date/time", f"g:{gid}:set:runat"),
    }[kind]
    running = group.status == QUEUED
    return _rows(
        kind_row[:2], kind_row[2:],
        [value_button],
        [btn(f"🎲 Jitter: {format_duration(group.jitter_seconds)}", f"g:{gid}:set:jitter"),
         btn(f"🌍 {group.timezone_name or 'UTC'}", f"g:{gid}:set:tz")],
        [btn(f"🌙 Quiet: {format_hhmm(group.quiet_start)}–{format_hhmm(group.quiet_end)}",
             f"g:{gid}:set:quiet")],
        [btn("🚦 Start window", f"g:{gid}:set:startat"),
         btn("🏁 End window", f"g:{gid}:set:endat")],
        [btn(f"🔢 Max posts: {group.max_posts or '∞'}", f"g:{gid}:set:maxposts"),
         btn(f"↺ Sent: {group.posts_sent or 0}", f"g:{gid}:resetcount")],
        [btn("⏸ Pause" if running else "▶️ Activate",
             f"g:{gid}:{'pause' if running else 'activate'}")],
        back_to(f"g:{gid}:menu"),
    )


def options_menu(group) -> InlineKeyboardMarkup:
    gid = group.id
    return _rows(
        [btn(f"🔕 Silent {_flag(group.silent)}", f"g:{gid}:opt:silent"),
         btn(f"🛡 Protect {_flag(group.protect_content)}", f"g:{gid}:opt:protect_content")],
        [btn(f"📌 Pin {_flag(group.pin_post)}", f"g:{gid}:opt:pin_post"),
         btn(f"🔀 Shuffle {_flag(group.shuffle)}", f"g:{gid}:opt:shuffle")],
        [btn(f"📮 One item per post {_flag(group.one_per_post)}",
             f"g:{gid}:opt:one_per_post")],
        [btn(f"🔘 Buttons: {_button_layout(group)}", f"g:{gid}:opt:buttons_attach")],
        [btn(f"🔁 Rotate targets {_flag(group.rotate_targets)}", f"g:{gid}:opt:rotate_targets"),
         btn(f"🔔 Alerts {_flag(group.notify_owner)}", f"g:{gid}:opt:notify_owner")],
        [btn(f"🧹 Auto-delete: {format_duration(group.delete_after)}", f"g:{gid}:set:delafter"),
         btn(f"🅰️ {group.caption_mode or 'HTML'}", f"g:{gid}:cyclemode")],
        [btn("🏷 Tags", f"g:{gid}:set:tags")],
        back_to(f"g:{gid}:menu"),
    )


# --------------------------------------------------------------------------
# Queues
# --------------------------------------------------------------------------

def queue_list(queues, page: int, total: int) -> InlineKeyboardMarkup:
    rows = [
        [btn(f"{STATUS_ICON.get(status, '•')} {truncate(label, 28)} · {count} items",
             f"q:{qid}:menu")]
        for qid, label, status, count in queues
    ]
    return _rows(
        *rows,
        paginator("queues", page, total),
        [btn("➕ New queue", "nav:newqueue")],
        back_to("nav:main"),
    )


def queue_menu(queue, item_count: int) -> InlineKeyboardMarkup:
    qid = queue.id
    running = queue.status == QUEUED
    kind = queue.schedule_kind or KIND_INTERVAL
    value_button = {
        KIND_INTERVAL: btn(f"⏱ Every {format_duration(queue.interval_seconds)}", f"q:{qid}:set:interval"),
        KIND_CRON: btn(f"🗓 {queue.cron_expr or '—'}", f"q:{qid}:set:cron"),
        KIND_DAILY: btn(f"🌅 {queue.daily_times or '—'}", f"q:{qid}:set:daily"),
        KIND_ONCE: btn("📌 Once", f"q:{qid}:set:interval"),
    }[kind]
    return _rows(
        [btn(f"📋 Items ({item_count})", f"q:{qid}:items"), btn("➕ Add group", f"q:{qid}:additem")],
        [value_button, btn(f"🌍 {queue.timezone_name or 'UTC'}", f"q:{qid}:set:tz")],
        [btn(f"🔁 Loop {_flag(queue.loop)}", f"q:{qid}:opt:loop"),
         btn(f"🔀 Shuffle {_flag(queue.shuffle)}", f"q:{qid}:opt:shuffle")],
        [btn(f"🧹 Delete previous {_flag(queue.delete_previous)}", f"q:{qid}:opt:delete_previous")],
        [btn(f"🎲 Jitter: {format_duration(queue.jitter_seconds)}", f"q:{qid}:set:jitter"),
         btn(f"🌙 Quiet: {format_hhmm(queue.quiet_start)}–{format_hhmm(queue.quiet_end)}",
             f"q:{qid}:set:quiet")],
        [btn("⏭ Post next now", f"q:{qid}:step"), btn(f"↩️ Cursor: {queue.cursor or 0}", f"q:{qid}:resetcursor")],
        [btn("⏸ Pause" if running else "▶️ Activate", f"q:{qid}:{'pause' if running else 'activate'}"),
         btn("🏷 Rename", f"q:{qid}:rename")],
        [btn("🗑 Delete", f"q:{qid}:delete"), btn("🔄 Refresh", f"q:{qid}:menu")],
        back_to("nav:queues"),
    )


def queue_items_menu(queue_id: int, items) -> InlineKeyboardMarkup:
    rows = [
        [btn(f"{position + 1}. {truncate(label, 24)}", f"g:{gid}:menu"),
         btn("⬆️", f"qi:{item_id}:up"), btn("⬇️", f"qi:{item_id}:down"),
         btn("❌", f"qi:{item_id}:del")]
        for position, (item_id, gid, label) in enumerate(items)
    ]
    return _rows(
        *rows,
        [btn("➕ Add group", f"q:{queue_id}:additem")],
        back_to(f"q:{queue_id}:menu"),
    )


def pick_group_menu(groups, scope: str) -> InlineKeyboardMarkup:
    rows = [[btn(truncate(label, 34), f"{scope}:{gid}")] for gid, label in groups]
    return _rows(*rows, back_to("nav:queues"))


# --------------------------------------------------------------------------
# Settings, tools, confirmations
# --------------------------------------------------------------------------

def settings_menu(prefs) -> InlineKeyboardMarkup:
    return _rows(
        [btn(f"🌍 Timezone: {prefs.timezone_name or 'UTC'}", "s:tz")],
        [btn(f"⏱ Default interval: {format_duration(prefs.default_interval)}", "s:interval")],
        [btn(f"🎯 Default target: {prefs.default_target or '—'}", "s:target")],
        [btn(f"🔔 Notify on post {_flag(prefs.notify_on_post)}", "s:toggle:notify_on_post")],
        [btn(f"⚠️ Notify on error {_flag(prefs.notify_on_error)}", "s:toggle:notify_on_error")],
        [btn(f"❓ Confirm destructive {_flag(prefs.confirm_destructive)}", "s:toggle:confirm_destructive")],
        back_to("nav:main"),
    )


def tools_menu() -> InlineKeyboardMarkup:
    return _rows(
        [btn("📜 Scheduler jobs", "x:jobs"), btn("🧾 History", "x:history")],
        [btn("❤️ Health", "x:health"), btn("📊 Stats", "nav:stats")],
        [btn("⬇️ Export JSON", "x:export"), btn("⬆️ Import JSON", "x:import")],
        [btn("💾 Backup database", "x:backup"), btn("📣 Broadcast", "x:broadcast")],
        [btn("🔑 Admins", "x:admins"), btn("🆔 Get chat ID", "x:id")],
        [btn("⏸ Pause everything", "x:pauseall"), btn("▶️ Resume everything", "x:resumeall")],
        back_to("nav:main"),
    )


def admins_menu(admins) -> InlineKeyboardMarkup:
    rows = [
        [btn(f"{'👑' if root else '👤'} {uid}{' @' + name if name else ''}", "noop")]
        + ([] if root else [btn("❌", f"x:deladmin:{uid}")])
        for uid, name, root in admins
    ]
    return _rows(*rows, [btn("➕ Add admin", "x:addadmin")], back_to("nav:tools"))


def confirm(action: str, target: str, back: str) -> InlineKeyboardMarkup:
    return _rows(
        [btn("✅ Yes, do it", f"ok:{action}:{target}"), btn("✖️ Cancel", back)],
    )


def help_menu(topics: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    rows = [[btn(title, f"help:{key}")] for key, title in topics]
    return _rows(*rows, back_to("nav:main"))


def cancel_kb() -> InlineKeyboardMarkup:
    return _rows([btn("✖️ Cancel", "nav:cancel")])


def simple_back(data: str) -> InlineKeyboardMarkup:
    return _rows(back_to(data), [btn("🏠 Menu", "nav:main")])
