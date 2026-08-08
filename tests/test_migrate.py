"""Build a v1-schema database, then check init_db upgrades it in place."""
import sqlite3

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(tempfile.mkdtemp(prefix="nebulabot-test-"))
os.environ["BOT_TOKEN"] = "123:TEST"
os.environ["ADMIN_IDS"] = "1"

con = sqlite3.connect("bot.db")
con.executescript("""
CREATE TABLE content_groups (
    id INTEGER NOT NULL PRIMARY KEY, owner_id INTEGER, caption TEXT,
    buttons_json TEXT, target_chat_id VARCHAR, status VARCHAR,
    interval_seconds INTEGER, next_run_at DATETIME, created_at DATETIME
);
CREATE TABLE media_items (
    id INTEGER NOT NULL PRIMARY KEY, group_id INTEGER, file_id VARCHAR,
    media_type VARCHAR, created_at DATETIME
);
INSERT INTO content_groups VALUES (1, 555, 'Legacy caption',
    '[[{"text":"Go","url":"https://example.com"}]]', '-1001234567890',
    'queued', 3600, NULL, '2026-01-01 00:00:00');
INSERT INTO media_items VALUES (1, 1, 'FILEID_A', 'photo', '2026-01-01 00:00:00');
INSERT INTO media_items VALUES (2, 1, 'FILEID_B', 'video', '2026-01-01 00:00:00');
""")
con.commit()
con.close()

from db import init_db, SessionLocal, ContentGroup, MediaItem, Target

init_db()
db = SessionLocal()
g = db.get(ContentGroup, 1)
assert g.caption == "Legacy caption", g.caption
assert g.status == "queued"
assert g.interval_seconds == 3600
assert g.schedule_kind is None or g.schedule_kind == "interval", g.schedule_kind
assert g.buttons() == [[{"text": "Go", "url": "https://example.com"}]]
targets = db.query(Target).filter(Target.group_id == 1).all()
assert len(targets) == 1 and targets[0].chat_id == "-1001234567890", targets
assert db.query(MediaItem).count() == 2
print("migration ok — legacy row intact, target backfilled, new columns present")

# Second run must be a no-op, not a duplicate-column error.
init_db()
assert db.query(Target).filter(Target.group_id == 1).count() == 1
print("idempotent re-init ok")
db.close()
