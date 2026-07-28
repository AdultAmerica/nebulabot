import asyncio
import json
import logging
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    InputMediaPhoto, InputMediaVideo
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import BOT_TOKEN, ADMIN_IDS, WEBHOOK_URL, WEBHOOK_SECRET, HOST, PORT
from db import SessionLocal, init_db, ContentGroup, MediaItem

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone="UTC")

CURRENT_GROUP = {}  # admin_id -> group_id
STATE = {}          # admin_id -> state dict


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def keyboard_from_json(raw: str | None):
    if not raw:
        return None
    data = json.loads(raw)
    rows = []
    for row in data:
        rows.append([InlineKeyboardButton(text=b["text"], url=b.get("url")) for b in row])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def schedule_group(group_id: int):
    db = SessionLocal()
    try:
        g = db.query(ContentGroup).filter(ContentGroup.id == group_id).first()
        if not g or not g.interval_seconds:
            return
        g.next_run_at = datetime.utcnow() + timedelta(seconds=g.interval_seconds)
        db.commit()
        next_run_at = g.next_run_at
    finally:
        db.close()

    scheduler.add_job(
        post_group,
        "date",
        run_date=next_run_at,
        args=[group_id],
        id=f"group_{group_id}_{next_run_at.timestamp()}",
        replace_existing=False,
    )


async def post_group(group_id: int):
    db = SessionLocal()
    try:
        g = db.query(ContentGroup).filter(ContentGroup.id == group_id).first()
        if not g or not g.target_chat_id:
            return
        items = (
            db.query(MediaItem)
            .filter(MediaItem.group_id == group_id)
            .order_by(MediaItem.id)
            .all()
        )
        if not items:
            return

        # Snapshot everything we need while the session is still open,
        # since ORM attributes become unusable once the session closes.
        caption = g.caption
        buttons_json = g.buttons_json
        target_chat_id = g.target_chat_id
        interval_seconds = g.interval_seconds
        item_data = [(i.file_id, i.media_type) for i in items]
    finally:
        db.close()

    # --- Album overflow handling: split into chunks of 10 ---
    chunks = [item_data[i:i + 10] for i in range(0, len(item_data), 10)]
    total_chunks = len(chunks)

    for chunk_index, chunk in enumerate(chunks):
        media_group = []

        for idx, (file_id, media_type) in enumerate(chunk):
            # These are frozen pydantic models, so the caption has to be
            # supplied at construction rather than assigned afterwards.
            fields = {"media": file_id}

            # Caption only on first item of first chunk
            if chunk_index == 0 and idx == 0 and caption:
                if total_chunks > 1:
                    fields["caption"] = (
                        f"{caption}\n\n<b>Part {chunk_index + 1}/{total_chunks}</b>"
                    )
                else:
                    fields["caption"] = caption
                fields["parse_mode"] = ParseMode.HTML

            if media_type == "photo":
                m = InputMediaPhoto(**fields)
            else:
                m = InputMediaVideo(**fields)

            media_group.append(m)

        await bot.send_media_group(target_chat_id, media_group)

        # Buttons only after first chunk
        if chunk_index == 0:
            kb = keyboard_from_json(buttons_json)
            if kb:
                await bot.send_message(target_chat_id, "🔘 Buttons:", reply_markup=kb)

    if interval_seconds:
        await schedule_group(group_id)


def admin_main_menu():
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ New group", callback_data="new_group")],
            [InlineKeyboardButton(text="📂 Select group", callback_data="select_group")],
            [InlineKeyboardButton(text="📜 List groups", callback_data="list_groups")],
        ]
    )
    return kb


def group_menu(group_id: int):
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🖼 Add media", callback_data=f"gm_addmedia_{group_id}")],
            [InlineKeyboardButton(text="✏️ Caption", callback_data=f"gm_caption_{group_id}")],
            [InlineKeyboardButton(text="🔗 Buttons", callback_data=f"gm_buttons_{group_id}")],
            [InlineKeyboardButton(text="🎯 Target chat", callback_data=f"gm_target_{group_id}")],
            [InlineKeyboardButton(text="⏱ Interval", callback_data=f"gm_interval_{group_id}")],
            [InlineKeyboardButton(text="📤 Post now", callback_data=f"gm_postnow_{group_id}")],
            [InlineKeyboardButton(text="📆 Schedule", callback_data=f"gm_schedule_{group_id}")],
        ]
    )
    return kb


@dp.message(CommandStart())
async def start(message: Message):
    if not is_admin(message.from_user.id):
        return await message.answer("Not authorized.")
    await message.answer("Admin panel:", reply_markup=admin_main_menu())


@dp.callback_query(F.data == "new_group")
async def cb_new_group(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("Not authorized.", show_alert=True)

    db = SessionLocal()
    try:
        g = ContentGroup(owner_id=call.from_user.id, status="draft")
        db.add(g)
        db.commit()
        db.refresh(g)
        gid = g.id
    finally:
        db.close()

    CURRENT_GROUP[call.from_user.id] = gid
    await call.message.edit_text(
        f"New group #{gid} created.\nSend photos/videos to add.\nUse buttons to edit.",
        reply_markup=group_menu(gid),
    )
    await call.answer()


@dp.callback_query(F.data == "select_group")
async def cb_select_group(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("Not authorized.", show_alert=True)

    db = SessionLocal()
    try:
        groups = (
            db.query(ContentGroup)
            .filter(ContentGroup.owner_id == call.from_user.id)
            .order_by(ContentGroup.id.desc())
            .all()
        )
        group_data = [(g.id, g.status) for g in groups[:10]]
    finally:
        db.close()

    if not group_data:
        await call.answer("No groups yet.", show_alert=True)
        return

    rows = [
        [InlineKeyboardButton(text=f"#{gid} ({status})", callback_data=f"sel_{gid}")]
        for gid, status in group_data
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await call.message.edit_text("Select a group:", reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data.startswith("sel_"))
async def cb_sel_group(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("Not authorized.", show_alert=True)

    gid = int(call.data.split("_")[1])
    CURRENT_GROUP[call.from_user.id] = gid
    await call.message.edit_text(f"Group #{gid} selected.", reply_markup=group_menu(gid))
    await call.answer()


@dp.callback_query(F.data == "list_groups")
async def cb_list_groups(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("Not authorized.", show_alert=True)

    db = SessionLocal()
    try:
        groups = (
            db.query(ContentGroup)
            .filter(ContentGroup.owner_id == call.from_user.id)
            .order_by(ContentGroup.id.desc())
            .all()
        )
        # Count media items while the session is still open to avoid
        # touching a lazy-loaded relationship after the session closes.
        lines = []
        for g in groups:
            count = (
                db.query(MediaItem)
                .filter(MediaItem.group_id == g.id)
                .count()
            )
            lines.append(f"#{g.id} – {g.status}, items: {count}")
    finally:
        db.close()

    if not lines:
        await call.answer("No groups.", show_alert=True)
        return

    text = "Your groups:\n" + "\n".join(lines)
    await call.message.edit_text(text, reply_markup=admin_main_menu())
    await call.answer()


@dp.message(F.photo | F.video)
async def add_media(message: Message):
    if not is_admin(message.from_user.id):
        return
    gid = CURRENT_GROUP.get(message.from_user.id)
    if not gid:
        return await message.answer("Select or create a group from the panel first.")

    db = SessionLocal()
    try:
        g = db.query(ContentGroup).filter(ContentGroup.id == gid).first()
        if not g:
            return await message.answer("Group not found. Create or select a new one.")

        if message.photo:
            file_id = message.photo[-1].file_id
            mtype = "photo"
        else:
            file_id = message.video.file_id
            mtype = "video"

        item = MediaItem(group_id=g.id, file_id=file_id, media_type=mtype)
        db.add(item)
        db.commit()
    finally:
        db.close()

    await message.answer(f"Added {mtype} to group #{gid}")


@dp.callback_query(F.data.startswith("gm_addmedia_"))
async def cb_addmedia(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("Not authorized.", show_alert=True)

    gid = int(call.data.split("_")[-1])
    CURRENT_GROUP[call.from_user.id] = gid
    await call.message.answer(f"Send photos/videos now — they'll be added to group #{gid}.")
    await call.answer()


@dp.callback_query(F.data.startswith("gm_caption_"))
async def cb_caption(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("Not authorized.", show_alert=True)

    gid = int(call.data.split("_")[-1])
    CURRENT_GROUP[call.from_user.id] = gid
    STATE[call.from_user.id] = {"mode": "edit_caption"}
    await call.message.answer("Send new caption (HTML allowed):")
    await call.answer()


@dp.callback_query(F.data.startswith("gm_target_"))
async def cb_target(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("Not authorized.", show_alert=True)

    gid = int(call.data.split("_")[-1])
    CURRENT_GROUP[call.from_user.id] = gid
    STATE[call.from_user.id] = {"mode": "edit_target"}
    await call.message.answer("Send target chat ID (e.g. -1001234567890):")
    await call.answer()


@dp.callback_query(F.data.startswith("gm_interval_"))
async def cb_interval(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("Not authorized.", show_alert=True)

    gid = int(call.data.split("_")[-1])
    CURRENT_GROUP[call.from_user.id] = gid
    STATE[call.from_user.id] = {"mode": "edit_interval"}
    await call.message.answer("Send interval in seconds (e.g. 3600):")
    await call.answer()


@dp.callback_query(F.data.startswith("gm_buttons_"))
async def cb_buttons(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("Not authorized.", show_alert=True)

    gid = int(call.data.split("_")[-1])
    CURRENT_GROUP[call.from_user.id] = gid
    STATE[call.from_user.id] = {
        "mode": "buttons_builder",
        "rows": [],
        "current_row": []
    }
    await call.message.answer(
        "🧩 Multi‑Row Button Builder\n\n"
        "Send:\n"
        "• `ROW` → start a new row\n"
        "• `Text | URL` → add button to current row\n"
        "• `DONE` → finish and save\n"
    )
    await call.answer()


@dp.callback_query(F.data.startswith("gm_postnow_"))
async def cb_postnow(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("Not authorized.", show_alert=True)

    gid = int(call.data.split("_")[-1])
    await post_group(gid)

    db = SessionLocal()
    try:
        g = db.query(ContentGroup).filter(ContentGroup.id == gid).first()
        if g:
            g.status = "posted"
            db.commit()
    finally:
        db.close()

    await call.answer("Posted.", show_alert=True)


@dp.callback_query(F.data.startswith("gm_schedule_"))
async def cb_schedule(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("Not authorized.", show_alert=True)

    gid = int(call.data.split("_")[-1])

    db = SessionLocal()
    try:
        g = db.query(ContentGroup).filter(ContentGroup.id == gid).first()
        if not g:
            return await call.answer("Group not found.", show_alert=True)
        if not g.interval_seconds:
            return await call.answer("Set an interval first.", show_alert=True)
        g.status = "queued"
        db.commit()
    finally:
        db.close()

    await schedule_group(gid)
    await call.answer("Scheduled.", show_alert=True)


@dp.message()
async def handle_state(message: Message):
    if not is_admin(message.from_user.id):
        return

    st = STATE.get(message.from_user.id)
    if not st:
        return

    gid = CURRENT_GROUP.get(message.from_user.id)
    if not gid:
        await message.answer("No group selected.")
        STATE.pop(message.from_user.id, None)
        return

    mode = st["mode"]

    if mode == "edit_caption":
        db = SessionLocal()
        try:
            g = db.query(ContentGroup).filter(ContentGroup.id == gid).first()
            if not g:
                STATE.pop(message.from_user.id, None)
                return await message.answer("Group no longer exists.")
            g.caption = message.text
            db.commit()
        finally:
            db.close()
        STATE.pop(message.from_user.id, None)
        await message.answer(f"Caption updated for group #{gid}.")
        await message.answer("Group menu:", reply_markup=group_menu(gid))
        return

    if mode == "edit_target":
        db = SessionLocal()
        try:
            g = db.query(ContentGroup).filter(ContentGroup.id == gid).first()
            if not g:
                STATE.pop(message.from_user.id, None)
                return await message.answer("Group no longer exists.")
            g.target_chat_id = message.text.strip()
            db.commit()
        finally:
            db.close()
        STATE.pop(message.from_user.id, None)
        await message.answer(f"Target chat set for group #{gid}.")
        await message.answer("Group menu:", reply_markup=group_menu(gid))
        return

    if mode == "edit_interval":
        try:
            sec = int(message.text.strip())
            if sec <= 0:
                raise ValueError
        except ValueError:
            await message.answer("Invalid number. Send a positive number of seconds.")
            return

        db = SessionLocal()
        try:
            g = db.query(ContentGroup).filter(ContentGroup.id == gid).first()
            if not g:
                STATE.pop(message.from_user.id, None)
                return await message.answer("Group no longer exists.")
            g.interval_seconds = sec
            db.commit()
        finally:
            db.close()
        STATE.pop(message.from_user.id, None)
        await message.answer(f"Interval {sec}s set for group #{gid}.")
        await message.answer("Group menu:", reply_markup=group_menu(gid))
        return

    if mode == "buttons_builder":
        txt = message.text.strip()

        if txt.upper() == "DONE":
            if st["current_row"]:
                st["rows"].append(st["current_row"])

            if not st["rows"]:
                STATE.pop(message.from_user.id, None)
                await message.answer("No buttons added.")
                return

            raw = json.dumps(st["rows"])
            db = SessionLocal()
            try:
                g = db.query(ContentGroup).filter(ContentGroup.id == gid).first()
                if not g:
                    STATE.pop(message.from_user.id, None)
                    return await message.answer("Group no longer exists.")
                g.buttons_json = raw
                db.commit()
            finally:
                db.close()

            STATE.pop(message.from_user.id, None)
            await message.answer(f"Buttons saved for group #{gid}.")
            await message.answer("Group menu:", reply_markup=group_menu(gid))
            return

        if txt.upper() == "ROW":
            if st["current_row"]:
                st["rows"].append(st["current_row"])
            st["current_row"] = []
            await message.answer("Started a new row.")
            return

        if "|" not in txt:
            await message.answer("Use format: Text | URL, or send ROW or DONE.")
            return

        text_part, url_part = [x.strip() for x in txt.split("|", 1)]
        st["current_row"].append({"text": text_part, "url": url_part})
        await message.answer(f"Added button: {text_part} → {url_part}")
        return


async def on_startup(app):
    init_db()
    scheduler.start()
    if WEBHOOK_URL:
        await bot.set_webhook(WEBHOOK_URL, secret_token=WEBHOOK_SECRET or None)


async def on_shutdown(app):
    await bot.delete_webhook()
    scheduler.shutdown(wait=False)


async def run_polling():
    init_db()
    scheduler.start()
    # Clear any webhook left over from a previous webhook-mode deployment,
    # since Telegram refuses to serve getUpdates while one is registered.
    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)


def run_webhook():
    app = web.Application()
    SimpleRequestHandler(
        dp, bot, secret_token=WEBHOOK_SECRET or None
    ).register(app, path="/webhook")
    setup_application(app, dp, bot=bot)

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    web.run_app(app, host=HOST, port=PORT)


def main():
    # No timestamp in the format: journald and most log collectors add
    # their own.
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    # Webhook mode needs a public HTTPS URL. Without one, fall back to
    # polling, which works anywhere with only a bot token.
    if WEBHOOK_URL:
        logging.info("Starting in webhook mode on %s:%s", HOST, PORT)
        run_webhook()
    else:
        logging.info("Starting in polling mode for %d admin(s)", len(ADMIN_IDS))
        asyncio.run(run_polling())


if __name__ == "__main__":
    main()
