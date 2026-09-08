"""Frozen column mappings for the July 2026 reward data migrations.

These are deliberately independent of application models: a migration must
not SELECT columns introduced by later revisions. Never use for application IO.
"""
import secrets
from datetime import datetime, timezone
from sqlalchemy import Column, String, Integer, DateTime, Boolean
from sqlalchemy.orm import declarative_base

Base = declarative_base()


def _id():
    return "".join(secrets.choice("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz") for _ in range(16))


class User(Base):
    __tablename__ = "users"
    id = Column(String(16), primary_key=True)
    email = Column(String(100))
    username = Column(String(100))
    credits = Column(Integer)
    pro_expires_at = Column(DateTime(timezone=True))
    pro_level = Column(String(20))


class CreditLog(Base):
    __tablename__ = "credit_logs"
    id = Column(String(16), primary_key=True, default=_id)
    user_id = Column(String(16))
    amount = Column(Integer)
    action = Column(String(50))
    source = Column(String(100))
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class ForumNotification(Base):
    __tablename__ = "forum_notifications"
    id = Column(String(16), primary_key=True, default=_id)
    user_id = Column(String(16))
    sender_id = Column(String(16))
    type = Column(String(50))
    post_id = Column(String(16))
    comment_id = Column(String(16))
    is_read = Column(Boolean)
    created_at = Column(DateTime(timezone=True))
