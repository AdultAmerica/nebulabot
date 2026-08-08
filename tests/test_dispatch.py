"""Feed synthetic updates through the real dispatcher: auth, routing, FSM."""
import asyncio
from datetime import datetime, timezone

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(tempfile.mkdtemp(prefix="nebulabot-test-"))
os.environ["BOT_TOKEN"] = "123:TEST"
os.environ["ADMIN_IDS"] = "1"
os.environ["DATABASE_URL"] = "sqlite:///dispatch.db"
os.environ["THROTTLE_SECONDS"] = "0"

from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

import bot as botmod
from db import ContentGroup, SessionLocal, init_db

CALLS = []
_counter = [1000]


def _fake_message(chat_id=1, text="ok"):
    _counter[0] += 1
    return Message(
        message_id=_counter[0], date=datetime.now(timezone.utc),
        chat=Chat(id=chat_id, type="private"), text=text,
    )


class FakeSession:
    async def __call__(self, bot, method, timeout=None):
        CALLS.append(method)
        name = type(method).__name__
        if name.startswith(("Send", "Edit")) and "Text" not in name or name in (
            "SendMessage", "EditMessageText", "SendPhoto", "SendDocument"
        ):
            return _fake_message(getattr(method, "chat_id", 1),
                                 getattr(method, "text", None) or "")
        return True

    async def close(self):
        pass


init_db()
botmod.bot.session = FakeSession()
dp = botmod.build_dispatcher()
LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(LOOP)


ADMIN = User(id=1, is_bot=False, first_name="Admin")
STRANGER = User(id=999, is_bot=False, first_name="Nobody")
_uid = [0]


def message_update(user, text):
    _uid[0] += 1
    _counter[0] += 1
    return Update(update_id=_uid[0], message=Message(
        message_id=_counter[0], date=datetime.now(timezone.utc),
        chat=Chat(id=user.id, type="private"), from_user=user, text=text,
    ))


def callback_update(user, data):
    _uid[0] += 1
    _counter[0] += 1
    return Update(update_id=_uid[0], callback_query=CallbackQuery(
        id=str(_uid[0]), from_user=user, chat_instance="x", data=data,
        message=Message(message_id=_counter[0], date=datetime.now(timezone.utc),
                        chat=Chat(id=user.id, type="private"), text="panel"),
    ))


def feed(update):
    CALLS.clear()
    LOOP.run_until_complete(dp.feed_update(botmod.bot, update))
    return list(CALLS)


def sent_text(calls):
    return "\n".join(
        getattr(c, "text", "") or "" for c in calls
        if isinstance(c, (SendMessage, EditMessageText))
    )


# --- 1. Strangers get nothing ----------------------------------------------
assert feed(message_update(STRANGER, "/start")) == []
calls = feed(callback_update(STRANGER, "nav:groups"))
assert len(calls) == 1 and isinstance(calls[0], AnswerCallbackQuery)
assert "authoris" in calls[0].text.lower()
print("1 ok: non-admin messages ignored, button presses politely refused")

# --- 2. /start reaches the panel -------------------------------------------
calls = feed(message_update(ADMIN, "/start"))
text = sent_text(calls)
assert "NebulaBot" in text, text
markup = [c for c in calls if isinstance(c, SendMessage)][0].reply_markup
labels = [b.text for row in markup.inline_keyboard for b in row]
assert any("New group" in l for l in labels), labels
print("2 ok: /start renders the main menu")

# --- 3. Creating a group by button, then navigating ------------------------
calls = feed(callback_update(ADMIN, "nav:newgroup"))
assert any(isinstance(c, EditMessageText) for c in calls), calls
db = SessionLocal()
gid = db.query(ContentGroup).order_by(ContentGroup.id.desc()).first().id
db.close()

for view in (f"g:{gid}:media", f"g:{gid}:targets", f"g:{gid}:buttons",
             f"g:{gid}:sched", f"g:{gid}:opts", f"g:{gid}:menu",
             "nav:groups", "nav:queues", "nav:settings", "nav:tools",
             "nav:stats", "help:index", "help:schedule", "help:commands"):
    calls = feed(callback_update(ADMIN, view))
    assert any(isinstance(c, EditMessageText) for c in calls), (view, calls)
    assert any(isinstance(c, AnswerCallbackQuery) for c in calls), view
print("3 ok: 14 panel screens route and redraw")

# --- 4. Prompt -> typed value -> saved -------------------------------------
calls = feed(callback_update(ADMIN, f"g:{gid}:caption"))
assert "Caption" in sent_text(calls), sent_text(calls)
feed(message_update(ADMIN, "Hello <b>there</b>"))
db = SessionLocal()
assert db.get(ContentGroup, gid).caption == "Hello <b>there</b>"
db.close()
print("4 ok: caption prompt captures the next message, formatting intact")

# --- 5. A bad value keeps the prompt open ----------------------------------
feed(callback_update(ADMIN, f"g:{gid}:set:interval"))
calls = feed(message_update(ADMIN, "banana"))
assert "⚠️" in sent_text(calls), sent_text(calls)
feed(message_update(ADMIN, "45m"))
db = SessionLocal()
assert db.get(ContentGroup, gid).interval_seconds == 2700
db.close()
print("5 ok: invalid value re-prompts instead of aborting")

# --- 6. A command escapes an open prompt -----------------------------------
feed(callback_update(ADMIN, f"g:{gid}:caption"))
calls = feed(message_update(ADMIN, "/menu"))
assert "NebulaBot" in sent_text(calls), sent_text(calls)
db = SessionLocal()
assert db.get(ContentGroup, gid).caption == "Hello <b>there</b>"  # unchanged
db.close()
print("6 ok: a command interrupts a prompt instead of being stored as the value")

# --- 7. Toggles flip and redraw the right screen ---------------------------
for field, screen in (("silent", "opts"), ("shuffle", "media"),
                      ("rotate_targets", "targets")):
    data = f"g:{gid}:opt:{field}" + (f":{screen}" if screen != "opts" else "")
    calls = feed(callback_update(ADMIN, data))
    assert any(isinstance(c, EditMessageText) for c in calls), (field, calls)
db = SessionLocal()
g = db.get(ContentGroup, gid)
assert g.silent and g.shuffle and g.rotate_targets
db.close()
print("7 ok: option toggles persist and redraw their originating screen")

# --- 8. Commands with arguments --------------------------------------------
feed(message_update(ADMIN, f"/target {gid} -1001234567890"))
feed(message_update(ADMIN, f"/tag {gid} promo,vip"))
calls = feed(message_update(ADMIN, f"/interval {gid} 3h"))
assert "✅" in sent_text(calls), sent_text(calls)
db = SessionLocal()
g = db.get(ContentGroup, gid)
assert g.interval_seconds == 10800 and g.tags == "promo,vip"
db.close()
print("8 ok: /target, /tag and /interval apply by id")

# --- 9. Activation through the panel ---------------------------------------
calls = feed(callback_update(ADMIN, f"g:{gid}:activate"))
answer = [c for c in calls if isinstance(c, AnswerCallbackQuery)][0]
assert "Active" in answer.text, answer.text
db = SessionLocal(); assert db.get(ContentGroup, gid).status == "queued"; db.close()
import scheduling
assert any(j.id.startswith(f"grp:{gid}:") for j in scheduling.scheduler.get_jobs())
calls = feed(callback_update(ADMIN, f"g:{gid}:pause"))
db = SessionLocal(); assert db.get(ContentGroup, gid).status == "paused"; db.close()
assert not any(j.id.startswith(f"grp:{gid}:") for j in scheduling.scheduler.get_jobs())
print("9 ok: activate registers a job, pause removes it")

# --- 10. Queues end to end --------------------------------------------------
feed(callback_update(ADMIN, "nav:newqueue"))
from db import Queue, QueueItem
db = SessionLocal(); qid = db.query(Queue).order_by(Queue.id.desc()).first().id; db.close()
calls = feed(callback_update(ADMIN, f"q:{qid}:additem"))
assert "rota" in sent_text(calls).lower(), sent_text(calls)
feed(callback_update(ADMIN, f"qpick:{qid}:{gid}"))
db = SessionLocal()
assert db.query(QueueItem).filter(QueueItem.queue_id == qid).count() == 1
db.close()
calls = feed(callback_update(ADMIN, f"q:{qid}:activate"))
answer = [c for c in calls if isinstance(c, AnswerCallbackQuery)][0]
assert "Active" in answer.text, answer.text
assert any(j.id.startswith(f"que:{qid}:") for j in scheduling.scheduler.get_jobs())
print("10 ok: queue built, populated and activated from buttons")

# --- 11. Confirmation before destruction ------------------------------------
calls = feed(callback_update(ADMIN, f"g:{gid}:delete"))
markup = [c for c in calls if isinstance(c, EditMessageText)][0].reply_markup
datas = [b.callback_data for row in markup.inline_keyboard for b in row]
assert f"ok:delgroup:{gid}" in datas, datas
db = SessionLocal(); assert db.get(ContentGroup, gid) is not None; db.close()
feed(callback_update(ADMIN, f"ok:delgroup:{gid}"))
db = SessionLocal(); assert db.get(ContentGroup, gid) is None; db.close()
assert not any(j.id.startswith(f"grp:{gid}:") for j in scheduling.scheduler.get_jobs())
print("11 ok: deletion asks first, then removes the row and its jobs")

# --- 12. Utility commands ---------------------------------------------------
for command, expect in (("/id", "chat ID"), ("/version", "NebulaBot"),
                        ("/whoami", "Admin"), ("/health", "Health"),
                        ("/stats", "Statistics"), ("/jobs", "Scheduler"),
                        ("/history", "History"), ("/settings", "Settings"),
                        ("/timezone Europe/Paris", "Europe/Paris"),
                        ("/help media", "albums"), ("/admins", "Admins"),
                        ("/pauseall", "Paused"), ("/groups", "Groups")):
    calls = feed(message_update(ADMIN, command))
    assert expect.lower() in sent_text(calls).lower(), (command, sent_text(calls)[:200])
print("12 ok: 13 utility commands answer")

# --- 13. A handler crash is reported, not swallowed ------------------------
import handlers.groups as gh
original = gh.views.group_view
gh.views.group_view = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
calls = feed(callback_update(ADMIN, "g:424242:menu"))
gh.views.group_view = original
assert any(isinstance(c, AnswerCallbackQuery) for c in calls), calls
print("13 ok: a failing handler still answers the button")

print("\nALL DISPATCH TESTS PASSED")
