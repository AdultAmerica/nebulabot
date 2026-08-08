"""Entry points, navigation, and the built-in manual."""

import time
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import config
import keyboards as kb
import views
from access import is_admin, user_tz
from handlers.ui import show_view, swap
from utils import esc

router = Router(name="common")

WELCOME = (
    "🌌 <b>NebulaBot</b>\n\n"
    "Compose a post once — media, caption, buttons — point it at your channels, "
    "and let it go out on a schedule.\n\n"
    "• <b>Groups</b> are single posts that can repeat.\n"
    "• <b>Queues</b> walk a list of groups, one per tick.\n\n"
    "Start with ➕ <b>New group</b>, or press ❓ <b>Help</b> for the full tour."
)

HELP_TOPICS = [
    ("start", "🚀 Getting started"),
    ("groups", "📂 Groups"),
    ("media", "🖼 Media & albums"),
    ("targets", "🎯 Targets & channels"),
    ("buttons", "🔘 Buttons"),
    ("schedule", "⏱ Scheduling"),
    ("queues", "🔁 Queues"),
    ("options", "🎛 Delivery options"),
    ("commands", "⌨️ Command reference"),
    ("trouble", "🩺 Troubleshooting"),
]

HELP = {
    "index": (
        "❓ <b>Help</b>\n\n"
        "Everything the panel can do is also a command, and everything a command "
        "does is also a button. Pick a topic."
    ),
    "start": (
        "🚀 <b>Getting started</b>\n\n"
        "<b>1.</b> Add this bot to your channel as an administrator with "
        "<i>Post messages</i> permission.\n"
        "<b>2.</b> Run <code>/id</code> inside that channel, or forward one of its "
        "messages here, to learn its chat ID.\n"
        "<b>3.</b> Press ➕ <b>New group</b> here in DM.\n"
        "<b>4.</b> Send the photos or videos you want in the post — they are "
        "added in the order you send them.\n"
        "<b>5.</b> Set a <b>Caption</b>, add <b>Targets</b>, then open "
        "<b>Schedule</b> and choose an interval.\n"
        "<b>6.</b> Press ▶️ <b>Activate</b>. Use 👁 <b>Preview</b> first to see "
        "exactly what subscribers will get.\n\n"
        "Set your timezone in ⚙️ <b>Settings</b> before using clock times."
    ),
    "groups": (
        "📂 <b>Groups</b>\n\n"
        "A group is one post plus the rules for delivering it: media, caption, "
        "buttons, target chats, schedule and options.\n\n"
        "Status meanings:\n"
        "📝 <b>draft</b> — never posted, no schedule running\n"
        "🟢 <b>active</b> — a job is registered and will fire\n"
        "⏸ <b>paused</b> — configured but not firing\n"
        "📤 <b>posted</b> — sent manually at least once\n"
        "🏁 <b>finished</b> — hit its post limit or one-shot time\n\n"
        "🧬 <b>Clone</b> copies everything except the delivery history — the "
        "quickest way to run the same album on a second channel with a "
        "different schedule."
    ),
    "media": (
        "🖼 <b>Media &amp; albums</b>\n\n"
        "Photos, videos, GIFs, documents, audio and voice notes are all accepted. "
        "Send them to the bot while a group is selected and they attach in order.\n\n"
        "Telegram caps an album at 10 items, so larger groups are split into "
        "consecutive albums automatically. Photos and videos share an album; "
        "audio and documents album only among themselves; GIFs, voice notes, "
        "video notes and stickers always send on their own.\n\n"
        "The caption goes on the first message of the post. Reorder with ⬆️⬇️, "
        "drop an item with ❌, or turn on 🔀 <b>Shuffle</b> to reorder on every "
        "single run.\n\n"
        "A group with no media but a caption posts as a plain text message — "
        "useful for announcements."
    ),
    "targets": (
        "🎯 <b>Targets</b>\n\n"
        "A group can post to any number of chats. Add the bot as an admin there "
        "first, then add the target by ID (<code>-1001234567890</code>) or "
        "@username.\n\n"
        "For a forum topic, append the thread: <code>-1001234567890:42</code>.\n\n"
        "Toggle a target off to skip it without losing it. Turn on 🔁 "
        "<b>Rotate targets</b> to send to one chat per run in turn, rather than "
        "all of them at once."
    ),
    "buttons": (
        "🔘 <b>Buttons</b>\n\n"
        "URL buttons are attached below the post. Add them one at a time, or "
        "use <b>Bulk edit</b> to lay out rows at once:\n\n"
        "<code>Join | https://t.me/example\n"
        "Site | https://a.com ;; Help | https://b.com</code>\n\n"
        "One line is one row; <code>;;</code> splits a row into columns.\n\n"
        "<b>Where the buttons land.</b> Telegram allows no keyboard on an "
        "album — <code>sendMediaGroup</code> has no field for one — so it "
        "depends on how many items the group holds:\n\n"
        "• <b>One photo or video</b> — caption and buttons ride on it. A "
        "single self-contained post, nothing trailing. The cleanest promo.\n"
        "• <b>Several items</b> — the album goes out whole, then the caption "
        "and buttons follow in one message underneath. Always two messages; "
        "no arrangement avoids that.\n\n"
        "🔘 <b>Buttons</b> in Options switches the second case to <i>on first "
        "item</i>, which splits the first item off to carry the keyboard."
    ),
    "schedule": (
        "⏱ <b>Scheduling</b>\n\n"
        "Four modes:\n"
        "• <b>Interval</b> — fixed spacing (<code>30m</code>, <code>6h</code>, <code>1d</code>)\n"
        "• <b>Cron</b> — <code>minute hour day month weekday</code>, e.g. "
        "<code>0 */4 * * *</code>\n"
        "• <b>Daily</b> — clock times, e.g. <code>09:00, 18:30</code>\n"
        "• <b>Once</b> — a single future moment, then the group retires\n\n"
        "Refinements:\n"
        "🎲 <b>Jitter</b> spreads each run by a random amount so posts do not "
        "look machine-timed.\n"
        "🌙 <b>Quiet hours</b> skip runs inside a window, wrapping past midnight.\n"
        "🚦🏁 <b>Window</b> keeps the schedule dormant before a date and retires "
        "it after another.\n"
        "🔢 <b>Max posts</b> stops automatically after N successful sends.\n\n"
        "Missed runs during downtime collapse into one and are only delivered if "
        "recent, so a restart posts once instead of dumping a backlog."
    ),
    "queues": (
        "🔁 <b>Queues</b>\n\n"
        "Where a group reposts the same album forever, a queue rotates through "
        "several groups — one per tick.\n\n"
        "Build one: <b>New queue</b> → add groups → set an interval → Activate. "
        "The member groups do not need schedules of their own; each posts to its "
        "own targets with its own caption and buttons.\n\n"
        "🔁 <b>Loop</b> restarts at the top when the rota ends; with it off the "
        "queue finishes.\n"
        "🔀 <b>Shuffle</b> picks at random instead of in order.\n"
        "🧹 <b>Delete previous</b> removes the last post before sending the next "
        "— a rolling single listing.\n"
        "⏭ <b>Post next now</b> fires one step by hand without disturbing the "
        "timer."
    ),
    "options": (
        "🎛 <b>Delivery options</b>\n\n"
        "🔕 <b>Silent</b> — no notification sound.\n"
        "🛡 <b>Protect</b> — subscribers cannot forward or save the post.\n"
        "📌 <b>Pin</b> — pin the first message of each post.\n"
        "🧹 <b>Auto-delete</b> — remove the post after a set delay.\n"
        "🔔 <b>Alerts</b> — DM you when a scheduled post fails.\n"
        "🅰️ <b>Parse mode</b> — HTML, MarkdownV2, Markdown or none.\n\n"
        "Auto-delete needs the bot to still be an admin when the timer fires, "
        "and Telegram will not let a bot delete another user's message older "
        "than 48 hours."
    ),
    "commands": (
        "⌨️ <b>Commands</b>\n\n"
        "<b>Panel</b>\n"
        "/start /menu /help /cancel\n\n"
        "<b>Groups</b>\n"
        "/new [name] · /groups · /group &lt;id&gt; · /find &lt;text&gt;\n"
        "/caption &lt;id&gt; &lt;text&gt; · /target &lt;id&gt; &lt;chat&gt; · /tag &lt;id&gt; &lt;tags&gt;\n"
        "/interval &lt;id&gt; &lt;30m&gt; · /cron &lt;id&gt; &lt;expr&gt; · /daily &lt;id&gt; &lt;09:00&gt;\n"
        "/preview &lt;id&gt; · /postnow &lt;id&gt; · /schedule &lt;id&gt; · /pause &lt;id&gt; · /resume &lt;id&gt;\n"
        "/clone &lt;id&gt; · /delete &lt;id&gt; · /purge &lt;id&gt;\n\n"
        "<b>Queues</b>\n"
        "/queues · /newqueue [name] · /queue &lt;id&gt;\n"
        "/qadd &lt;qid&gt; &lt;gid&gt; · /qdel &lt;qid&gt; &lt;gid&gt; · /qnext &lt;qid&gt;\n"
        "/qstart &lt;id&gt; · /qstop &lt;id&gt;\n\n"
        "<b>Tools</b>\n"
        "/settings · /timezone &lt;zone&gt; · /stats · /history · /jobs · /health\n"
        "/export [id] · /import · /backup · /broadcast &lt;text&gt;\n"
        "/admins · /addadmin &lt;id&gt; · /deladmin &lt;id&gt;\n"
        "/pauseall · /resumeall · /id · /ping · /version"
    ),
    "trouble": (
        "🩺 <b>Troubleshooting</b>\n\n"
        "<b>“chat not found”</b> — the bot is not in that chat, or the ID is "
        "wrong. Channel IDs start with <code>-100</code>. Run <code>/id</code> "
        "in the chat itself.\n\n"
        "<b>“not enough rights”</b> — make the bot an administrator with "
        "<i>Post messages</i>.\n\n"
        "<b>Nothing posted at the expected time</b> — check the group is 🟢 "
        "active, that it has a target, and that the run did not fall inside "
        "quiet hours. <code>/jobs</code> lists every registered job and its next "
        "fire time.\n\n"
        "<b>Posts stopped after a while</b> — check <b>Max posts</b> and the end "
        "of the window on the Schedule screen.\n\n"
        "<b>Caption shows raw tags</b> — the parse mode does not match the "
        "markup. Cycle 🅰️ on the Options screen.\n\n"
        "<code>/history</code> shows the last deliveries with the exact error "
        "Telegram returned."
    ),
}


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(WELCOME, reply_markup=kb.main_menu())


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("🏠 <b>NebulaBot</b>\n\nPick a section.", reply_markup=kb.main_menu())


@router.message(Command("help"))
async def cmd_help(message: Message, command):
    topic = (command.args or "index").strip().lower()
    text = HELP.get(topic, HELP["index"])
    await message.answer(text, reply_markup=kb.help_menu(HELP_TOPICS))


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Cancelled.", reply_markup=kb.main_menu())


@router.message(Command("id"))
async def cmd_id(message: Message):
    """Report the ids involved here — the fastest way to find a channel id."""
    lines = [
        f"👤 Your user ID: <code>{message.from_user.id}</code>",
        f"💬 This chat ID: <code>{message.chat.id}</code>  ({message.chat.type})",
    ]
    if message.message_thread_id:
        lines.append(f"🧵 Topic thread ID: <code>{message.message_thread_id}</code>")
    origin = message.forward_from_chat
    if origin:
        lines.append(f"📡 Forwarded from: <code>{origin.id}</code> — {esc(origin.title or '')}")
        lines.append("\nUse that ID as a target.")
    else:
        lines.append("\nForward a message from a channel here to read its ID.")
    await message.answer("\n".join(lines))


@router.message(Command("ping"))
async def cmd_ping(message: Message):
    start = time.monotonic()
    sent = await message.answer("🏓 …")
    await sent.edit_text(f"🏓 Pong — round trip {int((time.monotonic() - start) * 1000)} ms")


@router.message(Command("version"))
async def cmd_version(message: Message):
    await message.answer(
        f"🌌 NebulaBot <b>{config.VERSION}</b>\n"
        f"Mode: {'webhook' if config.WEBHOOK_URL else 'polling'}\n"
        f"Timezone default: <code>{esc(config.DEFAULT_TIMEZONE)}</code>"
    )


@router.message(Command("whoami"))
async def cmd_whoami(message: Message):
    tz = user_tz(message.from_user.id)
    await message.answer(
        f"👤 <code>{message.from_user.id}</code> @{esc(message.from_user.username or '—')}\n"
        f"Admin: {'yes' if is_admin(message.from_user.id) else 'no'}\n"
        f"Your timezone: <code>{esc(str(tz))}</code>\n"
        f"Local time: {esc(datetime.now(tz).strftime('%Y-%m-%d %H:%M %Z'))}"
    )


# --------------------------------------------------------------------------
# Navigation callbacks
# --------------------------------------------------------------------------

@router.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery):
    await call.answer()


@router.callback_query(F.data == "nav:cancel")
async def cb_cancel(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    await show_view(call, data.get("back", "nav:main"), call.from_user.id)
    await call.answer("Cancelled.")


@router.callback_query(F.data.in_({"nav:main", "nav:groups", "nav:queues",
                                   "nav:settings", "nav:tools", "nav:stats"}))
async def cb_nav(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_view(call, call.data, call.from_user.id)
    await call.answer()


@router.callback_query(F.data.startswith("page:"))
async def cb_page(call: CallbackQuery):
    _, scope, page = call.data.split(":", 2)
    page = int(page)
    if scope == "groups":
        text, markup = views.group_list_view(call.from_user.id, page)
    elif scope == "queues":
        text, markup = views.queue_list_view(call.from_user.id, page)
    elif scope.startswith("media_"):
        text, markup = views.media_view(int(scope.split("_")[1]), page)
    else:
        return await call.answer()
    await swap(call, text, markup)
    await call.answer()


@router.callback_query(F.data.startswith("help:"))
async def cb_help(call: CallbackQuery):
    topic = call.data.split(":", 1)[1]
    await swap(call, HELP.get(topic, HELP["index"]), kb.help_menu(HELP_TOPICS))
    await call.answer()
