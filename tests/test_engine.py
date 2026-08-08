"""Exercise the delivery engine against a stub Bot, plus triggers and parsing."""
import asyncio
import json
import types
from datetime import datetime, timezone

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(tempfile.mkdtemp(prefix="nebulabot-test-"))
os.environ["BOT_TOKEN"] = "123:TEST"
os.environ["ADMIN_IDS"] = "1"
os.environ["DATABASE_URL"] = "sqlite:///engine.db"

import scheduling
import utils
from sqlalchemy import text
from db import init_db, SessionLocal, ContentGroup, MediaItem, Target, Queue, QueueItem, PostLog

init_db()

SENT = []


class Msg:
    _n = 0
    def __init__(self):
        Msg._n += 1
        self.message_id = Msg._n


class FakeBot:
    async def send_media_group(self, chat_id, media, **kw):
        SENT.append(("album", chat_id, [(type(m).__name__, m.media, getattr(m, "caption", None)) for m in media], kw))
        return [Msg() for _ in media]

    async def _solo(self, kind, chat_id, file_id, kw):
        SENT.append((kind, chat_id, file_id, kw.get("caption"), bool(kw.get("reply_markup"))))
        return Msg()

    def __getattr__(self, name):
        if name.startswith("send_"):
            kind = name[5:]
            async def sender(chat_id, **kw):
                file_id = kw.pop(kind, None) or kw.pop("text", None)
                return await self._solo(kind, chat_id, file_id, kw)
            return sender
        raise AttributeError(name)

    async def pin_chat_message(self, **kw):
        SENT.append(("pin", kw["chat_id"], kw["message_id"]))

    async def delete_message(self, chat_id, message_id):
        SENT.append(("delete", chat_id, message_id))

    # Stands in for a Telegram that allows a keyboard to be edited onto an
    # album member. The refusing variant is defined at the test that needs it.
    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup):
        SENT.append(("keyboard", chat_id, message_id, None, bool(reply_markup)))
        return True

    async def edit_message_caption(self, chat_id, message_id, **kw):
        SENT.append(("uncaption", chat_id, message_id, None, False))
        return True


scheduling.attach_bot(FakeBot())


def make_group(**kw):
    db = SessionLocal()
    try:
        g = ContentGroup(owner_id=1, caption_mode="HTML", **kw)
        db.add(g); db.commit()
        return g.id
    finally:
        db.close()


def add_media(gid, specs):
    db = SessionLocal()
    try:
        for i, (fid, mtype) in enumerate(specs, 1):
            db.add(MediaItem(group_id=gid, file_id=fid, media_type=mtype, position=i))
        db.commit()
    finally:
        db.close()


def add_target(gid, chat, thread=None):
    db = SessionLocal()
    try:
        db.add(Target(group_id=gid, chat_id=chat, thread_id=thread, enabled=True)); db.commit()
    finally:
        db.close()


LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(LOOP)


def run(coro):
    return LOOP.run_until_complete(coro)


async def _start_scheduler():
    # APScheduler binds to the running loop, so it must start from inside one.
    scheduling.scheduler.start()


# --- 1. 23 photos: split into 10 / 10 / 3, caption only on the very first ---
SENT.clear()
gid = make_group(caption="Hello <b>world</b>")
add_media(gid, [(f"P{i}", "photo") for i in range(23)])
add_target(gid, "-100111")
ok, errors, sent = run(scheduling.post_group(gid, trigger="manual"))
assert ok == 1 and not errors, (ok, errors)
albums = [s for s in SENT if s[0] == "album"]
assert [len(a[2]) for a in albums] == [10, 10, 3], [len(a[2]) for a in albums]
captions = [c for a in albums for (_, _, c) in a[2] if c]
assert captions == ["Hello <b>world</b>"], captions
print("1 ok: 23 photos -> 10/10/3, single caption")

# --- 2. Mixed families never share an album ---
SENT.clear()
gid = make_group()
add_media(gid, [("P1", "photo"), ("V1", "video"), ("D1", "document"),
                ("D2", "document"), ("A1", "audio"), ("G1", "animation"),
                ("P2", "photo")])
add_target(gid, "-100222")
run(scheduling.post_group(gid))
shapes = [(s[0], len(s[2]) if s[0] == "album" else s[2]) for s in SENT]
assert shapes == [("album", 2), ("album", 2), ("audio", "A1"),
                  ("animation", "G1"), ("photo", "P2")], shapes
print("2 ok: photo+video album, document album, audio/gif/photo solo")

# --- 3. Text-only group ---
SENT.clear()
gid = make_group(caption="Just an announcement",
                 buttons_json='[[{"text":"Go","url":"https://e.com"}]]')
add_target(gid, "-100333")
run(scheduling.post_group(gid))
# send_message puts the body in the file_id slot of the stub, index 2.
assert SENT[0][0] == "message" and SENT[0][2] == "Just an announcement" and SENT[0][4] is True, SENT
assert len(SENT) == 1
print("3 ok: caption-only group posts one message carrying the keyboard")

# --- 4. Single media carries caption AND keyboard, no follow-up ---
SENT.clear()
gid = make_group(caption="Solo", buttons_json='[[{"text":"Go","url":"https://e.com"}]]')
add_media(gid, [("P9", "photo")])
add_target(gid, "-100444")
run(scheduling.post_group(gid))
assert len(SENT) == 1 and SENT[0][0] == "photo" and SENT[0][3] == "Solo" and SENT[0][4] is True, SENT
print("4 ok: single item carries caption and buttons directly")

# --- 5. One post: album whole, keyboard edited onto it, no trailing message ---
SENT.clear()
gid = make_group(caption="Album", buttons_json='[[{"text":"Go","url":"https://e.com"}]]')
add_media(gid, [("P1", "photo"), ("P2", "photo"), ("P3", "photo")])
add_target(gid, "-100555")
run(scheduling.post_group(gid))
assert [s[0] for s in SENT] == ["album", "keyboard"], SENT
assert len(SENT[0][2]) == 3, "the album must not be split"
assert [c for (_, _, c) in SENT[0][2] if c] == ["Album"], "caption rides on the album"
assert SENT[1][4] is True, "the edit must carry the keyboard"
assert scheduling.album_keyboard_supported() is True
assert not [s for s in SENT if s[0] == "message"], "nothing trails a successful attach"
print("5 ok: album keeps caption and gains the keyboard — a single post")

# --- 5a. When Telegram refuses, fall back and never ask again -------------
from aiogram.exceptions import TelegramBadRequest
class NoAlbumKeyboard(FakeBot):
    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup):
        SENT.append(("keyboard-refused", chat_id, message_id, None, False))
        raise TelegramBadRequest(method=types.SimpleNamespace(),
                                 message="BUTTON_TYPE_INVALID")
scheduling._album_keyboard_loaded = False          # forget the probe
db = SessionLocal(); db.execute(text("DELETE FROM app_state")); db.commit(); db.close()
scheduling.attach_bot(NoAlbumKeyboard())
SENT.clear()
run(scheduling.post_group(gid))
# The caption is stripped back off the album so it can travel with the
# buttons — the probe post looks like every post that follows it.
assert [s[0] for s in SENT] == ["album", "keyboard-refused", "uncaption", "message"], SENT
assert SENT[3][2] == "Album" and SENT[3][4] is True, SENT[3]
assert scheduling.album_keyboard_supported() is False
SENT.clear()
run(scheduling.post_group(gid))
# Verdict remembered: no second attempt, and the caption moves to the message.
assert [s[0] for s in SENT] == ["album", "message"], SENT
assert not [c for (_, _, c) in SENT[0][2] if c], "caption now travels with the buttons"
assert SENT[1][2] == "Album" and SENT[1][4] is True, SENT[1]
print("5a ok: refusal is remembered; caption moves down with the buttons")

scheduling.attach_bot(FakeBot())
scheduling._album_keyboard_loaded = False
db = SessionLocal(); db.execute(text("DELETE FROM app_state")); db.commit(); db.close()

# --- 5b. Opting in splits the first item off to carry the keyboard --------
db = SessionLocal(); db.get(ContentGroup, gid).buttons_attach = True; db.commit(); db.close()
SENT.clear()
run(scheduling.post_group(gid))
assert [s[0] for s in SENT] == ["photo", "album"], SENT
assert SENT[0][2] == "P1" and SENT[0][3] == "Album" and SENT[0][4] is True, SENT[0]
assert len(SENT[1][2]) == 2, SENT[1]
assert not [c for (_, _, c) in SENT[1][2] if c], "album must not repeat the caption"
assert not [s for s in SENT if s[0] == "message"], "no trailing bubble in this mode"
print("5b ok: opt-in mode puts caption+buttons on the first media item")

# --- 5c. The one-time migration moves existing groups to the new default --
db = SessionLocal()
db.execute(text("UPDATE content_groups SET buttons_attach = 1"))
db.execute(text("DELETE FROM schema_meta"))
db.commit(); db.close()
init_db()
db = SessionLocal()
flags = [g.buttons_attach for g in db.query(ContentGroup).all()]
db.close()
assert not any(flags), flags
print("5c ok: one-time migration flips existing groups to 'below album'")

# --- 5d. No buttons: album keeps its caption, nothing trails ---------------
SENT.clear()
gid = make_group(caption="Plain")
add_media(gid, [("Q1", "photo"), ("Q2", "photo")])
add_target(gid, "-100PLAIN")
run(scheduling.post_group(gid))
assert [s[0] for s in SENT] == ["album"], SENT
assert [c for (_, _, c) in SENT[0][2] if c] == ["Plain"], SENT[0]
print("5d ok: without buttons the album keeps its caption and sends alone")

# --- 6. Fan-out to every enabled target, then rotation ---
SENT.clear()
gid = make_group(caption="Fan")
add_media(gid, [("P1", "photo")])
for chat in ("-100A", "-100B", "-100C"):
    add_target(gid, chat)
ok, _, _ = run(scheduling.post_group(gid))
assert ok == 3 and sorted(s[1] for s in SENT) == ["-100A", "-100B", "-100C"], SENT

db = SessionLocal(); db.get(ContentGroup, gid).rotate_targets = True; db.commit(); db.close()
hits = []
for _ in range(4):
    SENT.clear()
    run(scheduling.post_group(gid))
    hits.append(SENT[0][1])
assert hits == ["-100A", "-100B", "-100C", "-100A"], hits
print("6 ok: fan-out to all targets; rotation cycles one per run")

# --- 7. Pin + auto-delete scheduling ---
SENT.clear()
run(_start_scheduler())
gid = make_group(caption="Pinned", pin_post=True, delete_after=60)
add_media(gid, [("P1", "photo")])
add_target(gid, "-100PIN")
run(scheduling.post_group(gid))
assert any(s[0] == "pin" for s in SENT), SENT
assert any(j.id.startswith("del:") for j in scheduling.scheduler.get_jobs()), scheduling.scheduler.get_jobs()
print("7 ok: post pinned and a deletion job registered")

# --- 8. max_posts retires the group ---
gid = make_group(caption="Limited", max_posts=2, status="queued", interval_seconds=3600,
                 schedule_kind="interval")
add_target(gid, "-100LIM")
scheduling.sync_group(gid)
assert any(j.id.startswith(f"grp:{gid}:") for j in scheduling.scheduler.get_jobs())
run(scheduling.post_group(gid, trigger="schedule"))
run(scheduling.post_group(gid, trigger="schedule"))
db = SessionLocal(); g = db.get(ContentGroup, gid); status = g.status; db.close()
assert status == "done", status
assert not any(j.id.startswith(f"grp:{gid}:") for j in scheduling.scheduler.get_jobs())
print("8 ok: post limit retires the group and drops its jobs")

# --- 9. Failure is captured, not raised ---
SENT.clear()
gid = make_group(caption="Broken")
add_target(gid, "-100BAD")
class Boom(FakeBot):
    async def send_message(self, **kw):
        from aiogram.exceptions import TelegramBadRequest
        raise TelegramBadRequest(method=types.SimpleNamespace(), message="chat not found")
scheduling.attach_bot(Boom())
ok, errors, _ = run(scheduling.post_group(gid))
assert ok == 0 and errors and "chat not found" in errors[0], (ok, errors)
db = SessionLocal(); assert db.query(PostLog).filter(PostLog.ok.is_(False)).count() >= 1; db.close()
scheduling.attach_bot(FakeBot())
print("9 ok: send failure recorded as an error, not an exception")

# --- 10. Queue walks its rota and advances the cursor ---
SENT.clear()
gids = []
for n in range(3):
    g = make_group(caption=f"Q{n}")
    add_target(g, "-100Q")
    gids.append(g)
db = SessionLocal()
q = Queue(owner_id=1, name="rota", status="queued", schedule_kind="interval",
          interval_seconds=3600, loop=True, cursor=0)
db.add(q); db.commit()
qid = q.id
for pos, g in enumerate(gids, 1):
    db.add(QueueItem(queue_id=qid, group_id=g, position=pos))
db.commit(); db.close()
scheduling.sync_queue(qid)
posted = []
for _ in range(4):
    SENT.clear()
    run(scheduling.run_queue_job(qid))
    posted.append(SENT[0][2])
assert posted == ["Q0", "Q1", "Q2", "Q0"], posted
print("10 ok: queue posts one group per tick and loops")

# --- 10b. delete_previous removes exactly the last tick's messages ---------
db = SessionLocal()
q = db.get(Queue, qid)
q.delete_previous = True
q.cursor = 0
q.last_message_ids = None      # start clean: nothing to clean up on the first tick
db.commit()
db.close()
SENT.clear()
run(scheduling.run_queue_job(qid))          # posts, nothing to clean yet
first = [s for s in SENT if s[0] == "message"]
assert first and not [s for s in SENT if s[0] == "delete"], SENT
db = SessionLocal()
stored = json.loads(db.get(Queue, qid).last_message_ids)
db.close()
assert len(stored) == 1 and stored[0][0] == "-100Q" and len(stored[0][1]) == 1, stored
SENT.clear()
run(scheduling.run_queue_job(qid))          # next tick deletes the previous one
deleted = [(s[1], s[2]) for s in SENT if s[0] == "delete"]
assert deleted == [(stored[0][0], stored[0][1][0])], (deleted, stored)
print("10b ok: delete_previous removes the prior tick's messages only")

# --- 11. Quiet hours ---
class Fake:
    quiet_start = 23 * 60; quiet_end = 7 * 60; timezone_name = "UTC"
f = Fake()
assert scheduling.in_quiet_hours(f, datetime(2026, 1, 1, 23, 30, tzinfo=timezone.utc))
assert scheduling.in_quiet_hours(f, datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc))
assert not scheduling.in_quiet_hours(f, datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))
f.quiet_start, f.quiet_end = 9 * 60, 17 * 60
assert scheduling.in_quiet_hours(f, datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))
assert not scheduling.in_quiet_hours(f, datetime(2026, 1, 1, 20, 0, tzinfo=timezone.utc))
print("11 ok: quiet hours handle wrapping and non-wrapping windows")

# --- 12. Triggers for every schedule kind ---
class S:
    schedule_kind = "interval"; interval_seconds = 900; cron_expr = None
    daily_times = None; run_at = None; jitter_seconds = 30
    timezone_name = "Europe/Berlin"; start_at = None; end_at = None
s = S()
assert len(scheduling.build_triggers(s)) == 1
s.schedule_kind = "cron"; s.cron_expr = "0 */4 * * *"
assert len(scheduling.build_triggers(s)) == 1
s.schedule_kind = "daily"; s.daily_times = "09:00,18:30"
assert len(scheduling.build_triggers(s)) == 2
s.schedule_kind = "once"; s.run_at = datetime(2030, 1, 1, 12, 0)
assert len(scheduling.build_triggers(s)) == 1
s.schedule_kind = "cron"; s.cron_expr = "not a cron"
assert scheduling.build_triggers(s) == []
assert scheduling.validate_cron("0 9 * * 1-5") is None
assert scheduling.validate_cron("bogus") is not None
print("12 ok: triggers build for all four kinds; bad cron rejected")

# --- 13. Parsing helpers ---
assert utils.parse_duration("30m") == 1800
assert utils.parse_duration("2h30m") == 9000
assert utils.parse_duration("1d 6h") == 108000
assert utils.parse_duration("90") == 90
assert utils.parse_duration("banana") is None
assert utils.parse_time_list("9:00, 18:30") == ["09:00", "18:30"]
assert utils.parse_time_list("25:00") is None
assert utils.parse_hhmm("22:30") == 1350
assert utils.parse_chat_id("-1001234567890") == "-1001234567890"
assert utils.parse_chat_id("https://t.me/mychannel") == "@mychannel"
assert utils.parse_chat_id("mychannel") == "@mychannel"
assert utils.parse_chat_id("nope!") is None
from zoneinfo import ZoneInfo
assert utils.parse_datetime("2030-05-01 12:00", ZoneInfo("UTC")) == datetime(2030, 5, 1, 12, 0)
assert utils.format_duration(9000) == "2h 30m"
print("13 ok: duration, time, chat-id and datetime parsing")

scheduling.scheduler.shutdown(wait=False)
print("\nALL ENGINE TESTS PASSED")
