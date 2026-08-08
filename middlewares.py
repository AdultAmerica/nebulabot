"""Cross-cutting update handling: authorisation, throttling, error capture."""

import logging
import time
from collections import defaultdict
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

import config
from access import is_admin

log = logging.getLogger(__name__)


class AuthMiddleware(BaseMiddleware):
    """Drop every update from someone who is not an admin.

    Silence rather than a refusal message for plain messages: a bot that
    answers strangers invites them to keep probing. Button presses get a reply
    because Telegram leaves the client spinning otherwise.
    """

    async def __call__(self, handler: Callable[[TelegramObject, dict], Awaitable[Any]],
                       event: TelegramObject, data: dict) -> Any:
        user = data.get("event_from_user")
        if is_admin(user.id if user else None):
            data["is_admin"] = True
            return await handler(event, data)

        if isinstance(event, CallbackQuery):
            await event.answer("Not authorised.", show_alert=True)
        elif isinstance(event, Message) and event.chat.type == "private":
            log.info("Ignored message from non-admin %s", user.id if user else "?")
        return None


class ThrottleMiddleware(BaseMiddleware):
    """Collapse bursts from a single admin so a stuck finger cannot spam."""

    def __init__(self, rate: float = config.THROTTLE_SECONDS):
        self.rate = rate
        self._last: dict[int, float] = defaultdict(float)

    async def __call__(self, handler, event: TelegramObject, data: dict) -> Any:
        user = data.get("event_from_user")
        if user and self.rate > 0:
            now = time.monotonic()
            if now - self._last[user.id] < self.rate:
                if isinstance(event, CallbackQuery):
                    await event.answer("Slow down a moment.")
                return None
            self._last[user.id] = now
        return await handler(event, data)


class CommandStateMiddleware(BaseMiddleware):
    """Let a command interrupt a prompt.

    Without this, typing /groups while the bot is waiting for a caption would
    be stored *as* the caption — the classic way FSM bots trap their users.
    """

    async def __call__(self, handler, event: TelegramObject, data: dict) -> Any:
        state = data.get("state")
        if isinstance(event, Message) and state and (event.text or "").startswith("/"):
            if await state.get_state() is not None:
                await state.set_state(None)
        return await handler(event, data)


class ErrorMiddleware(BaseMiddleware):
    """Keep a handler crash from swallowing the update silently.

    Without this a failed callback leaves the button spinning forever, which
    reads as "the bot is dead" even when only one action went wrong.
    """

    async def __call__(self, handler, event: TelegramObject, data: dict) -> Any:
        try:
            return await handler(event, data)
        except Exception as exc:
            log.exception("Handler failed on %s", type(event).__name__)
            try:
                if isinstance(event, CallbackQuery):
                    await event.answer(f"Error: {exc}"[:190], show_alert=True)
                elif isinstance(event, Message):
                    await event.answer(f"⚠️ Something went wrong:\n<code>{exc}</code>"[:3900])
            except Exception:
                pass
            return None
