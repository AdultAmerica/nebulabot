"""Who may drive the bot, and what they have configured."""

import config
from db import AdminUser, SessionLocal, UserPrefs

_cache: set[int] | None = None


def _load() -> set[int]:
    global _cache
    if _cache is None:
        db = SessionLocal()
        try:
            _cache = set(config.ADMIN_IDS) | {a.id for a in db.query(AdminUser).all()}
        finally:
            db.close()
    return _cache


def invalidate():
    global _cache
    _cache = None


def is_admin(user_id: int | None) -> bool:
    return bool(user_id) and user_id in _load()


def is_root(user_id: int | None) -> bool:
    """Bootstrap admins from .env — they cannot be removed from chat."""
    return bool(user_id) and user_id in config.ADMIN_IDS


def add_admin(user_id: int, username: str | None, added_by: int) -> bool:
    db = SessionLocal()
    try:
        if db.get(AdminUser, user_id):
            return False
        db.add(AdminUser(id=user_id, username=username, added_by=added_by))
        db.commit()
    finally:
        db.close()
    invalidate()
    return True


def remove_admin(user_id: int) -> bool:
    if is_root(user_id):
        return False
    db = SessionLocal()
    try:
        row = db.get(AdminUser, user_id)
        if not row:
            return False
        db.delete(row)
        db.commit()
    finally:
        db.close()
    invalidate()
    return True


def list_admins() -> list[tuple[int, str, bool]]:
    db = SessionLocal()
    try:
        rows = [(a.id, a.username or "", False) for a in db.query(AdminUser).order_by(AdminUser.id).all()]
    finally:
        db.close()
    roots = [(uid, "", True) for uid in config.ADMIN_IDS]
    seen = {uid for uid, _, _ in roots}
    return roots + [row for row in rows if row[0] not in seen]


def get_prefs(user_id: int) -> UserPrefs:
    """Fetch (creating on first use) a detached copy of the user's preferences."""
    db = SessionLocal()
    try:
        prefs = db.get(UserPrefs, user_id)
        if not prefs:
            prefs = UserPrefs(id=user_id, timezone_name=config.DEFAULT_TIMEZONE)
            db.add(prefs)
            db.commit()
        db.expunge(prefs)
        return prefs
    finally:
        db.close()


def set_pref(user_id: int, field: str, value) -> None:
    db = SessionLocal()
    try:
        prefs = db.get(UserPrefs, user_id)
        if not prefs:
            prefs = UserPrefs(id=user_id)
            db.add(prefs)
        setattr(prefs, field, value)
        db.commit()
    finally:
        db.close()


def user_tz(user_id: int):
    return config.tz_or_default(get_prefs(user_id).timezone_name)
