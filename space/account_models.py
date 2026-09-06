"""Cloud-owned spending authorizations and holds, not world state."""
import datetime as dt
from sqlalchemy import Column, String, Integer, Boolean, DateTime, ForeignKey, Index, LargeBinary, CheckConstraint
from database import Base


class SpaceCreditAuthorization(Base):
    __tablename__ = "space_credit_authorizations"
    id = Column(String(36), primary_key=True)
    user_id = Column(String(16), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    world_id = Column(String(36), nullable=False)
    entity_id = Column(String(36), nullable=False)
    request_digest = Column(LargeBinary(32), nullable=False)
    max_credits = Column(Integer, nullable=False)
    enabled = Column(Boolean, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: dt.datetime.now(dt.timezone.utc))
    __table_args__ = (Index("ix_space_credit_entity", "world_id", "entity_id"),
                     CheckConstraint("max_credits >= 0 AND max_credits <= 168", name="ck_space_credit_budget"))


class SpaceCreditReservation(Base):
    __tablename__ = "space_credit_reservations"
    id = Column(String(36), primary_key=True)
    authorization_id = Column(String(36), ForeignKey("space_credit_authorizations.id", ondelete="RESTRICT"), nullable=False, index=True)
    user_id = Column(String(16), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    state = Column(String(16), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: dt.datetime.now(dt.timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: dt.datetime.now(dt.timezone.utc))
    __table_args__ = (Index("ix_space_credit_user_state", "user_id", "state"),
                     CheckConstraint("state IN ('reserved','captured','released')", name="ck_space_credit_reservation_state"))
