"""Router assembly.

Order is behaviour, not taste:
  * `common` first so /start and /cancel always win;
  * `tools` before `groups` so an imported .json is not swallowed by the
    media collector;
  * `values` last so a prompted answer only lands there once every command
    and media filter has declined it.
"""

from aiogram import Dispatcher

from handlers import common, groups, queues, tools, values


def setup(dp: Dispatcher):
    for module in (common, tools, groups, queues, values):
        dp.include_router(module.router)
