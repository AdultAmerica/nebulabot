"""NebulaBot — scheduled media-group posting for Telegram channels.

Entry point: wires the dispatcher, middlewares and scheduler together and
picks a run mode. Behaviour lives in `handlers/`, delivery in `scheduling.py`.
"""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

import config
import handlers
import scheduling
from db import init_db
from middlewares import (
    AuthMiddleware, CommandStateMiddleware, ErrorMiddleware, ThrottleMiddleware,
)
from scheduling import scheduler

log = logging.getLogger(__name__)

if not config.BOT_TOKEN:
    raise SystemExit("BOT_TOKEN is not set. Copy .env.example to .env and fill it in.")

bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

# Old job pickles recorded this module path for the posting callable. Keeping
# the name here means a database from an earlier version still resolves its
# jobs instead of having them dropped on load.
post_group = scheduling.post_group

COMMANDS = [
    ("start", "Open the admin panel"),
    ("menu", "Main menu"),
    ("new", "Create a group"),
    ("groups", "List your groups"),
    ("group", "Open a group by id"),
    ("find", "Search groups"),
    ("preview", "Send yourself a preview"),
    ("postnow", "Post a group immediately"),
    ("schedule", "Activate a group's schedule"),
    ("pause", "Pause a group"),
    ("interval", "Set a group's interval"),
    ("cron", "Set a cron expression"),
    ("daily", "Set daily post times"),
    ("caption", "Set a caption"),
    ("target", "Add a target chat"),
    ("clone", "Duplicate a group"),
    ("delete", "Delete a group"),
    ("queues", "List queues"),
    ("newqueue", "Create a queue"),
    ("qadd", "Add a group to a queue"),
    ("qnext", "Post the next queue item"),
    ("stats", "Usage statistics"),
    ("history", "Recent deliveries"),
    ("jobs", "Scheduled jobs"),
    ("health", "Runtime health"),
    ("settings", "Your preferences"),
    ("timezone", "Set your timezone"),
    ("export", "Export groups as JSON"),
    ("import", "Import groups from JSON"),
    ("backup", "Download the database"),
    ("broadcast", "Message every target"),
    ("admins", "Manage admins"),
    ("id", "Show chat and user ids"),
    ("help", "Full manual"),
    ("cancel", "Abort the current prompt"),
]


def build_dispatcher():
    for observer in (dp.message, dp.callback_query):
        observer.outer_middleware(AuthMiddleware())
        observer.middleware(ThrottleMiddleware())
        observer.middleware(ErrorMiddleware())
    dp.message.middleware(CommandStateMiddleware())
    handlers.setup(dp)
    return dp


async def prepare():
    """Everything that must happen before the first update is served."""
    init_db()
    scheduling.attach_bot(bot)
    if not scheduler.running:
        scheduler.start()
    scheduling.resync_all()
    try:
        await bot.set_my_commands(
            [BotCommand(command=name, description=text) for name, text in COMMANDS],
            scope=BotCommandScopeAllPrivateChats(),
        )
    except Exception as exc:
        # A stale command list is cosmetic; never let it stop the bot booting.
        log.warning("Could not publish the command list: %s", exc)


async def on_startup(app):
    await prepare()
    await bot.set_webhook(
        config.WEBHOOK_URL,
        secret_token=config.WEBHOOK_SECRET or None,
        drop_pending_updates=False,
    )
    log.info("Webhook registered at %s", config.WEBHOOK_URL)


async def on_shutdown(app):
    await bot.delete_webhook()
    scheduler.shutdown(wait=False)
    await bot.session.close()


async def run_polling():
    await prepare()
    # Telegram refuses getUpdates while a webhook is registered, so clear any
    # left over from a previous webhook deployment.
    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


def run_webhook():
    app = web.Application()
    SimpleRequestHandler(
        dp, bot, secret_token=config.WEBHOOK_SECRET or None
    ).register(app, path=config.WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    async def health(request):
        return web.json_response({
            "ok": True,
            "version": config.VERSION,
            "jobs": len(scheduler.get_jobs()),
        })

    app.router.add_get("/health", health)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    web.run_app(app, host=config.HOST, port=config.PORT)


def main():
    # No timestamp in the format: journald and most log collectors add their own.
    logging.basicConfig(level=config.LOG_LEVEL, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)

    build_dispatcher()

    if config.WEBHOOK_URL:
        log.info("Starting NebulaBot %s in webhook mode on %s:%s",
                 config.VERSION, config.HOST, config.PORT)
        run_webhook()
    else:
        log.info("Starting NebulaBot %s in polling mode for %d bootstrap admin(s)",
                 config.VERSION, len(config.ADMIN_IDS))
        asyncio.run(run_polling())


if __name__ == "__main__":
    main()
