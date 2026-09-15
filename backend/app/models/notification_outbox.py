"""Notification outbox for idempotent WeChat reminders (migration 025)."""
from sqlalchemy import (
    BigInteger,
    Column,
    Date,
    ForeignKey,
    Integer,
    JSON,
    String,
    TIMESTAMP,
)
from sqlalchemy.sql import func

from app.database import Base


class NotificationOutbox(Base):
    __tablename__ = "notification_outbox"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    binding_id = Column(
        BigInteger,
        ForeignKey("user_notification_bindings.id", ondelete="CASCADE"),
        nullable=True,
    )
    web_push_subscription_id = Column(
        BigInteger,
        ForeignKey("web_push_subscriptions.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    notification_type = Column(String(32), nullable=False)
    local_date = Column(Date, nullable=False)
    dedupe_key = Column(String(191), nullable=False, unique=True)
    safe_payload_json = Column(JSON, nullable=False)
    status = Column(String(16), nullable=False, default="pending", server_default="pending")
    attempt_count = Column(Integer, nullable=False, default=0, server_default="0")
    max_attempts = Column(Integer, nullable=False, default=5, server_default="5")
    available_at = Column(TIMESTAMP, nullable=False, server_default=func.current_timestamp())
    locked_by = Column(String(128), nullable=True)
    locked_at = Column(TIMESTAMP, nullable=True)
    provider_message_id = Column(String(128), nullable=True)
    error_code = Column(String(64), nullable=True)
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.current_timestamp())
    updated_at = Column(TIMESTAMP, nullable=True, onupdate=func.current_timestamp())
    sent_at = Column(TIMESTAMP, nullable=True)
