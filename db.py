"""Schema, session factory, and a small forward-only migrator.

The bot ships as a single SQLite file that people already have data in, so the
schema evolves by adding columns and tables — never by dropping or renaming
them. `init_db()` is safe to run against a fresh database and against one
created by any earlier version.
"""

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey, Integer, String, Text,
    create_engine, inspect, text,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

from config import DATABASE_URL

log = logging.getLogger(__name__)

engine = create_engine(DATABASE_URL, echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
Base = declarative_base()


def utcnow() -> datetime:
    """Naive UTC. Every datetime column in this schema stores UTC."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# Statuses a group can hold.
DRAFT, QUEUED, PAUSED, POSTED, DONE = "draft", "queued", "paused", "posted", "done"

# Schedule kinds.
KIND_INTERVAL, KIND_CRON, KIND_DAILY, KIND_ONCE = "interval", "cron", "daily", "once"

# Media kinds we accept. Photo and video share albums; audio and document each
# album among themselves; the rest are always sent on their own.
ALBUM_FAMILIES = {
    "photo": "visual",
    "video": "visual",
    "audio": "audio",
    "document": "document",
}
SOLO_TYPES = ("animation", "voice", "video_note", "sticker")
MEDIA_TYPES = tuple(ALBUM_FAMILIES) + SOLO_TYPES


class ContentGroup(Base):
    """An album plus everything needed to deliver it repeatedly."""

    __tablename__ = "content_groups"

    id = Column(Integer, primary_key=True)
    owner_id = Column(Integer, index=True)
    name = Column(String)
    tags = Column(String, default="")

    caption = Column(Text)
    caption_mode = Column(String, default="HTML")  # HTML, Markdown, none
    buttons_json = Column(Text)

    # Superseded by the targets relationship, kept so old rows still post.
    target_chat_id = Column(String)

    status = Column(String, default=DRAFT)

    schedule_kind = Column(String, default=KIND_INTERVAL)
    interval_seconds = Column(Integer)
    cron_expr = Column(String)
    daily_times = Column(String)         # "09:00,18:30"
    run_at = Column(DateTime)            # one-shot, UTC
    jitter_seconds = Column(Integer, default=0)
    timezone_name = Column(String)

    start_at = Column(DateTime)          # UTC; schedule is dormant before this
    end_at = Column(DateTime)            # UTC; schedule retires after this
    quiet_start = Column(Integer)        # minutes past local midnight
    quiet_end = Column(Integer)
    max_posts = Column(Integer)
    posts_sent = Column(Integer, default=0)

    shuffle = Column(Boolean, default=False)
    rotate_targets = Column(Boolean, default=False)
    rotation_index = Column(Integer, default=0)

    silent = Column(Boolean, default=False)
    protect_content = Column(Boolean, default=False)
    pin_post = Column(Boolean, default=False)
    delete_after = Column(Integer)       # seconds; None = keep forever
    notify_owner = Column(Boolean, default=True)

    next_run_at = Column(DateTime)
    last_run_at = Column(DateTime)
    last_error = Column(Text)

    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    def buttons(self) -> list[list[dict]]:
        if not self.buttons_json:
            return []
        try:
            return json.loads(self.buttons_json)
        except (ValueError, TypeError):
            return []

    def tag_list(self) -> list[str]:
        return [t for t in (self.tags or "").split(",") if t]

    def label(self) -> str:
        return self.name or f"Group #{self.id}"


class Target(Base):
    """One destination chat for a group. A group can fan out to many."""

    __tablename__ = "targets"

    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey("content_groups.id"), index=True)
    chat_id = Column(String)
    title = Column(String)
    enabled = Column(Boolean, default=True)
    thread_id = Column(Integer)          # forum topic, when the chat has them
    created_at = Column(DateTime, default=utcnow)

    group = relationship("ContentGroup", backref="targets")

    def label(self) -> str:
        return self.title or str(self.chat_id)


class MediaItem(Base):
    __tablename__ = "media_items"

    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey("content_groups.id"), index=True)
    file_id = Column(String)
    file_unique_id = Column(String)      # stable across bots; used to dedupe
    media_type = Column(String)
    caption = Column(Text)               # per-item override, rarely used
    position = Column(Integer, default=0)
    created_at = Column(DateTime, default=utcnow)

    group = relationship("ContentGroup", backref="media_items")


class Queue(Base):
    """An ordered rota of groups posted one per tick.

    Where a group's own schedule reposts the same album forever, a queue walks
    a list: every fire posts the next group and advances the cursor. That is
    the difference between a repeating ad and a drip campaign.
    """

    __tablename__ = "queues"

    id = Column(Integer, primary_key=True)
    owner_id = Column(Integer, index=True)
    name = Column(String)
    status = Column(String, default=DRAFT)

    schedule_kind = Column(String, default=KIND_INTERVAL)
    interval_seconds = Column(Integer)
    cron_expr = Column(String)
    daily_times = Column(String)
    jitter_seconds = Column(Integer, default=0)
    timezone_name = Column(String)

    quiet_start = Column(Integer)
    quiet_end = Column(Integer)
    start_at = Column(DateTime)
    end_at = Column(DateTime)

    cursor = Column(Integer, default=0)
    loop = Column(Boolean, default=True)
    shuffle = Column(Boolean, default=False)
    delete_previous = Column(Boolean, default=False)

    posts_sent = Column(Integer, default=0)
    next_run_at = Column(DateTime)
    last_run_at = Column(DateTime)
    last_error = Column(Text)
    # What the previous tick sent, so `delete_previous` can clean it up:
    # JSON [[chat_id, [message_id, ...]], ...]
    last_message_ids = Column(Text)

    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    def label(self) -> str:
        return self.name or f"Queue #{self.id}"


class QueueItem(Base):
    __tablename__ = "queue_items"

    id = Column(Integer, primary_key=True)
    queue_id = Column(Integer, ForeignKey("queues.id"), index=True)
    group_id = Column(Integer, ForeignKey("content_groups.id"), index=True)
    position = Column(Integer, default=0)
    created_at = Column(DateTime, default=utcnow)


class PostLog(Base):
    """One row per delivery attempt, successful or not."""

    __tablename__ = "post_logs"

    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, index=True)
    queue_id = Column(Integer, index=True)
    chat_id = Column(String)
    message_ids = Column(Text)           # JSON list
    item_count = Column(Integer, default=0)
    ok = Column(Boolean, default=True)
    error = Column(Text)
    trigger = Column(String)             # schedule, manual, queue, broadcast
    created_at = Column(DateTime, default=utcnow, index=True)


class AdminUser(Base):
    __tablename__ = "admin_users"

    id = Column(Integer, primary_key=True)   # Telegram user id
    username = Column(String)
    added_by = Column(Integer)
    created_at = Column(DateTime, default=utcnow)


class UserPrefs(Base):
    __tablename__ = "user_prefs"

    id = Column(Integer, primary_key=True)   # Telegram user id
    timezone_name = Column(String)
    default_interval = Column(Integer, default=3600)
    default_target = Column(String)
    notify_on_post = Column(Boolean, default=False)
    notify_on_error = Column(Boolean, default=True)
    confirm_destructive = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utcnow)


def _migrate():
    """Add any columns and tables an older database is missing."""
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())

    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                ddl = f'ALTER TABLE {table.name} ADD COLUMN {col.name} {col.type.compile(engine.dialect)}'
                default = col.default.arg if col.default is not None and not callable(col.default.arg) else None
                if default is not None:
                    literal = f"'{default}'" if isinstance(default, str) else int(default) if isinstance(default, bool) else default
                    ddl += f" DEFAULT {literal}"
                conn.execute(text(ddl))
                log.info("Migrated: added %s.%s", table.name, col.name)


def _backfill_targets():
    """Turn the legacy single `target_chat_id` into a Target row."""
    db = SessionLocal()
    try:
        legacy = (
            db.query(ContentGroup)
            .filter(ContentGroup.target_chat_id.isnot(None))
            .filter(ContentGroup.target_chat_id != "")
            .all()
        )
        moved = 0
        for g in legacy:
            exists = (
                db.query(Target)
                .filter(Target.group_id == g.id, Target.chat_id == g.target_chat_id)
                .first()
            )
            if not exists:
                db.add(Target(group_id=g.id, chat_id=g.target_chat_id, enabled=True))
                moved += 1
        if moved:
            db.commit()
            log.info("Migrated %d legacy target(s) into the targets table", moved)
    finally:
        db.close()


def init_db():
    Base.metadata.create_all(bind=engine)
    _migrate()
    _backfill_targets()
