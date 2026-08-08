"""The single handler that receives every prompted value.

Registered last, so commands and media keep their own handlers; anything else
typed while a prompt is open lands here and is routed by the field recorded in
the FSM data.
"""

import logging

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

import views
from access import add_admin
from forms import apply_group, apply_queue, apply_settings
from handlers.ui import Form, show_view
from scheduling import broadcast
from utils import esc, rich_text, truncate

log = logging.getLogger(__name__)
router = Router(name="values")


@router.message(StateFilter(Form.value), F.text)
async def on_value(message: Message, state: FSMContext):
    data = await state.get_data()
    scope, oid, field, back = (
        data.get("scope"), data.get("oid"), data.get("field"), data.get("back", "nav:main"),
    )
    if not scope or not field:
        await state.clear()
        return

    user_id = message.from_user.id
    # Captions keep their formatting; everything else is a plain value.
    text = rich_text(message) if field == "caption" else message.text

    if scope == "g":
        ok, note = apply_group(oid, field, text, user_id)
    elif scope == "q":
        ok, note = apply_queue(oid, field, text, user_id)
    elif scope == "s":
        ok, note = apply_settings(user_id, field, text)
    elif scope == "x":
        ok, note, back = await _apply_tool(message, field, text, user_id, back)
    else:
        ok, note = False, "Nothing to do."

    if not ok:
        # Stay in the prompt so a typo can simply be retyped.
        return await message.answer(f"⚠️ {note}")

    await state.clear()
    await message.answer(f"✅ {note}")
    if back.startswith("find:"):
        text_out, markup = views.group_list_view(user_id, 0, back.split(":", 1)[1])
        return await message.answer(text_out, reply_markup=markup)
    await show_view(message, back, user_id)


async def _apply_tool(message: Message, field: str, text: str, user_id: int,
                      back: str) -> tuple[bool, str, str]:
    if field == "find":
        return True, f"Searching for “{esc(truncate(text, 40))}”.", f"find:{text.strip()}"

    if field == "addadmin":
        raw = text.strip()
        if not raw.lstrip("-").isdigit():
            return False, "Send a numeric Telegram user ID.", back
        added = add_admin(int(raw), None, user_id)
        return True, (f"{raw} can now use the bot." if added else f"{raw} was already an admin."), back

    if field == "broadcast":
        ok, errors = await broadcast(user_id, rich_text(message))
        note = f"Sent to {ok} chat(s)."
        if errors:
            note += " Failed: " + ", ".join(esc(truncate(e, 60)) for e in errors[:3])
        return True, note, back

    return False, f"Unknown action {field}.", back
