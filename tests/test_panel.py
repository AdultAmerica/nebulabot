"""Exercise the form appliers, every rendered view, and callback-data routing."""
import asyncio

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(tempfile.mkdtemp(prefix="nebulabot-test-"))
os.environ["BOT_TOKEN"] = "123:TEST"
os.environ["ADMIN_IDS"] = "1"
os.environ["DATABASE_URL"] = "sqlite:///panel.db"

import scheduling
import views
from db import init_db, SessionLocal, ContentGroup, MediaItem, Target
from forms import apply_group, apply_queue, apply_settings, PROMPTS, prompt_for
from handlers.groups import create_group, _clone, _toggle, _set_status, _reverse_media
from handlers.queues import create_queue, add_to_queue
from handlers.tools import build_export, restore_export

init_db()
LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(LOOP)
LOOP.run_until_complete(asyncio.sleep(0))
scheduling.scheduler.start() if False else None

USER = 1
gid = create_group(USER, "Promo album")
qid = create_queue(USER, "Evening rota")

# --- 1. Every group field, happy path -------------------------------------
good = [
    ("name", "Weekend promo"), ("caption", "<b>Big</b> news"),
    ("interval", "45m"), ("cron", "0 */6 * * *"), ("daily", "09:00, 21:30"),
    ("runat", "2030-01-01 10:00"), ("jitter", "2m"), ("tz", "Europe/Berlin"),
    ("quiet", "23:00-07:00"), ("startat", "2030-01-01"), ("endat", "2031-01-01"),
    ("maxposts", "12"), ("delafter", "6h"), ("tags", "promo, vip"),
    ("target", "-1001234567890"), ("addbtn", "Join | https://t.me/x"),
    ("bulkbtn", "A | https://a.com\nB | https://b.com ;; C | https://c.com"),
]
for field, value in good:
    ok, note = apply_group(gid, field, value, USER)
    assert ok, (field, note)

db = SessionLocal()
g = db.get(ContentGroup, gid)
assert g.name == "Weekend promo" and g.caption == "<b>Big</b> news"
assert g.interval_seconds == 2700 and g.cron_expr == "0 */6 * * *"
assert g.daily_times == "09:00,21:30" and g.jitter_seconds == 120
assert g.timezone_name == "Europe/Berlin"
assert (g.quiet_start, g.quiet_end) == (1380, 420)
assert g.max_posts == 12 and g.delete_after == 21600 and g.tags == "promo,vip"
assert g.start_at and g.end_at and g.run_at
assert [len(r) for r in g.buttons()] == [1, 2], g.buttons()
assert db.query(Target).filter(Target.group_id == gid).count() == 1
db.close()
print("1 ok: all 17 group fields applied and persisted")

# --- 2. Bad input is rejected without mutating -----------------------------
bad = [
    ("interval", "banana"), ("cron", "nonsense"), ("daily", "99:99"),
    ("tz", "Mars/Olympus"), ("quiet", "always"), ("maxposts", "-3"),
    ("target", "!!!"), ("addbtn", "no pipe"), ("bulkbtn", "no pipe either"),
    ("runat", "some day"),
]
for field, value in bad:
    ok, note = apply_group(gid, field, value, USER)
    assert not ok and note, (field, ok, note)
db = SessionLocal()
g = db.get(ContentGroup, gid)
assert g.interval_seconds == 2700 and g.cron_expr == "0 */6 * * *"
assert g.timezone_name == "Europe/Berlin" and g.max_posts == 12
db.close()
print("2 ok: 10 invalid inputs rejected, nothing overwritten")

# --- 3. Clearing works ------------------------------------------------------
for field in ("jitter", "quiet", "startat", "endat", "maxposts", "delafter"):
    ok, _ = apply_group(gid, field, "off", USER)
    assert ok, field
ok, _ = apply_group(gid, "caption", "off", USER)
db = SessionLocal()
g = db.get(ContentGroup, gid)
assert g.quiet_start is None and g.max_posts is None and g.delete_after is None
assert g.caption is None and g.jitter_seconds == 0
db.close()
apply_group(gid, "caption", "Back again", USER)
print("3 ok: 'off' clears optional fields")

# --- 4. Duplicate target refused -------------------------------------------
ok, note = apply_group(gid, "target", "-1001234567890", USER)
assert not ok and "already" in note, note
ok, _ = apply_group(gid, "target", "@second_channel", USER)
assert ok
ok, _ = apply_group(gid, "target", "-1001234567890:42", USER)
assert not ok  # same chat, already listed
print("4 ok: duplicate targets refused, @username accepted")

# --- 5. Queue + settings appliers ------------------------------------------
for field, value in [("name", "Nightly"), ("interval", "3h"), ("tz", "UTC"),
                     ("daily", "20:00"), ("quiet", "01:00-05:00"), ("jitter", "30s")]:
    ok, note = apply_queue(qid, field, value, USER)
    assert ok, (field, note)
assert apply_queue(qid, "interval", "nope", USER)[0] is False
for field, value in [("tz", "Asia/Tokyo"), ("default_interval", "12h"),
                     ("default_target", "-100999")]:
    ok, note = apply_settings(USER, field, value)
    assert ok, (field, note)
assert apply_settings(USER, "tz", "Nowhere/Here")[0] is False
print("5 ok: queue and settings appliers")

# --- 6. Activation guards ---------------------------------------------------
scheduling.attach_bot(None)
empty = create_group(USER)
# create_group seeds the owner's default target, so clear it to test the guard.
db = SessionLocal(); db.query(Target).filter(Target.group_id == empty).delete(); db.commit(); db.close()
ok, note = _set_status(empty, USER, "queued")
assert not ok and "target" in note.lower(), note
apply_group(empty, "target", "-100777", USER)
ok, note = _set_status(empty, USER, "queued")
assert not ok and ("media" in note.lower() or "caption" in note.lower()), note
apply_group(empty, "caption", "now postable", USER)
ok, note = _set_status(empty, USER, "queued")
assert ok, note
db = SessionLocal(); assert db.get(ContentGroup, empty).status == "queued"; db.close()
ok, note = _set_status(empty, USER, "paused")
assert ok
print("6 ok: activation refuses a group with no target or no content")

# --- 7. Clone copies content but not history -------------------------------
db = SessionLocal()
for i in range(3):
    db.add(MediaItem(group_id=gid, file_id=f"F{i}", media_type="photo", position=i + 1))
db.commit(); db.close()
clone_id = _clone(gid, USER)
db = SessionLocal()
src, dup = db.get(ContentGroup, gid), db.get(ContentGroup, clone_id)
assert dup.caption == src.caption and dup.buttons_json == src.buttons_json
assert dup.status == "draft" and (dup.posts_sent or 0) == 0
assert db.query(MediaItem).filter(MediaItem.group_id == clone_id).count() == 3
assert db.query(Target).filter(Target.group_id == clone_id).count() == 2
db.close()
print("7 ok: clone duplicates media, targets and settings; resets status")

# --- 8. Reordering ----------------------------------------------------------
_reverse_media(gid)
db = SessionLocal()
order = [m.file_id for m in db.query(MediaItem).filter(MediaItem.group_id == gid)
         .order_by(MediaItem.position, MediaItem.id).all()]
db.close()
assert order == ["F2", "F1", "F0"], order
assert _toggle(gid, "shuffle") is True and _toggle(gid, "shuffle") is False
print("8 ok: reverse and toggles")

# --- 9. Export / import round trip -----------------------------------------
add_to_queue(qid, gid, USER)
add_to_queue(qid, clone_id, USER)
payload = build_export(USER)
assert len(payload["groups"]) >= 3 and payload["queues"], payload.keys()
before = len(payload["groups"])
created, media_count = restore_export(payload, USER)
assert created == before and media_count >= 6, (created, media_count)
db = SessionLocal()
restored = db.query(ContentGroup).filter(ContentGroup.owner_id == USER).count()
assert restored == before * 2, restored
assert db.query(ContentGroup).filter(ContentGroup.status == "queued").count() == 0
db.close()
print(f"9 ok: exported {before} groups and re-imported them, all paused")

# --- 10. Every view renders -------------------------------------------------
screens = [
    views.group_list_view(USER, 0), views.group_list_view(USER, 0, "promo"),
    views.group_view(gid, USER), views.media_view(gid, 0), views.targets_view(gid),
    views.buttons_view(gid), views.schedule_view(gid, USER), views.options_view(gid),
    views.queue_list_view(USER, 0), views.queue_view(qid, USER),
    views.queue_items_view(qid), views.settings_view(USER), views.stats_view(USER),
    views.history_view(USER), views.jobs_view(), views.admins_view(),
    views.group_view(999999, USER), views.queue_view(999999, USER),
]
for text, markup in screens:
    assert isinstance(text, str) and text.strip()
    assert len(text) < 4096, len(text)
    assert markup is None or markup.inline_keyboard is not None
print(f"10 ok: {len(screens)} screens render inside Telegram's length limit")

# --- 11. Callback data is routable and within Telegram's 64-byte cap --------
ROUTES = ("nav:", "help:", "page:", "noop", "g:", "m:", "t:", "q:", "qi:",
          "qpick:", "s:", "x:", "ok:")
seen = set()
for _, markup in screens:
    if markup is None:
        continue
    for row in markup.inline_keyboard:
        for button in row:
            if button.url:
                continue
            data = button.callback_data
            assert data, button
            assert len(data.encode()) <= 64, (data, len(data))
            assert data.startswith(ROUTES), data
            seen.add(data.split(":")[0])
assert {"g", "q", "nav", "x", "s"} <= seen, seen
print(f"11 ok: every callback routable and ≤64 bytes ({len(seen)} scopes)")

# --- 12. Prompts exist for every field the panel can ask for ---------------
asked = set()
for _, markup in screens:
    if markup is None:
        continue
    for row in markup.inline_keyboard:
        for button in row:
            data = button.callback_data or ""
            parts = data.split(":")
            if len(parts) >= 4 and parts[2] == "set":
                asked.add(parts[3])
missing = asked - set(PROMPTS)
assert not missing, missing
for field in PROMPTS:
    assert prompt_for(field).strip()
print(f"12 ok: {len(asked)} prompt buttons all have written instructions")

print("\nALL PANEL TESTS PASSED")
