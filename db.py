from datetime import datetime
from sqlalchemy import (
    create_engine, Column, Integer, String, DateTime, Text, ForeignKey
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

engine = create_engine("sqlite:///bot.db", echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False)
Base = declarative_base()


class ContentGroup(Base):
    __tablename__ = "content_groups"

    id = Column(Integer, primary_key=True)
    owner_id = Column(Integer)
    caption = Column(Text)
    buttons_json = Column(Text)
    target_chat_id = Column(String)
    status = Column(String, default="draft")  # draft, queued, posted
    interval_seconds = Column(Integer)
    next_run_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)


class MediaItem(Base):
    __tablename__ = "media_items"

    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey("content_groups.id"))
    file_id = Column(String)
    media_type = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)

    group = relationship("ContentGroup", backref="media_items")


def init_db():
    Base.metadata.create_all(bind=engine)
