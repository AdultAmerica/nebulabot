"""Shared plumbing for handlers: screen swapping, prompts, and view routing."""

from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

import keyboards as kb
import views
from forms import prompt_for


class Form(StatesGroup):
    """One state for every "type a value" interaction.

    What is being edited lives in the FSM data rather than in the state name,
    which keeps a couple of dozen editable fields down to a single handler.
    """

    value = State()
    importing = State()


async def swap(call: CallbackQuery, text: str, markup=None):
    """Replace the panel in place, tolerating an identical redraw."""
    try:
        await call.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc):
            return
        # The panel was attached to a photo, or is too old to edit — start a
        # fresh one rather than losing the interaction.
        await call.message.answer(text, reply_markup=markup, disable_web_page_preview=True)


async def ask(call: CallbackQuery, state: FSMContext, scope: str, oid: int,
              field: str, back: str):
    """Prompt for a single value and remember where to return afterwards."""
    await state.set_state(Form.value)
    await state.update_data(scope=scope, oid=oid, field=field, back=back)
    await swap(call, prompt_for(field), kb.cancel_kb())
    await call.answer()


def resolve(view: str, user_id: int, page: int = 0):
    """Map a view token like `g:12:sched` back to its rendered screen."""
    if view == "nav:main":
        return "🏠 <b>NebulaBot</b>\n\nPick a section.", kb.main_menu()
    if view == "nav:groups":
        return views.group_list_view(user_id, page)
    if view == "nav:queues":
        return views.queue_list_view(user_id, page)
    if view == "nav:settings":
        return views.settings_view(user_id)
    if view == "nav:tools":
        return "🛠 <b>Tools</b>\n\nDiagnostics, backups and bulk actions.", kb.tools_menu()
    if view == "nav:stats":
        return views.stats_view(user_id)

    parts = view.split(":")
    if parts[0] == "g" and len(parts) >= 3:
        gid, action = int(parts[1]), parts[2]
        return {
            "menu": lambda: views.group_view(gid, user_id),
            "media": lambda: views.media_view(gid, page),
            "targets": lambda: views.targets_view(gid),
            "buttons": lambda: views.buttons_view(gid),
            "sched": lambda: views.schedule_view(gid, user_id),
            "opts": lambda: views.options_view(gid),
        }.get(action, lambda: views.group_view(gid, user_id))()
    if parts[0] == "q" and len(parts) >= 3:
        qid, action = int(parts[1]), parts[2]
        if action == "items":
            return views.queue_items_view(qid)
        return views.queue_view(qid, user_id)

    return "🏠 <b>NebulaBot</b>", kb.main_menu()


async def show_view(target: CallbackQuery | Message, view: str, user_id: int, page: int = 0):
    text, markup = resolve(view, user_id, page)
    if isinstance(target, CallbackQuery):
        await swap(target, text, markup)
    else:
        await target.answer(text, reply_markup=markup, disable_web_page_preview=True)
