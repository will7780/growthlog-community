"""Encrypted Web Push device subscriptions (migration 026)."""
from sqlalchemy import (
    BigInteger,
    Column,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    TIMESTAMP,
)
from sqlalchemy.sql import func

from app.database import Base


class WebPushSubscription(Base):
    __tablename__ = "web_push_subscriptions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    subscription_ciphertext = Column(LargeBinary(4096), nullable=False)
    endpoint_hash = Column(String(64), nullable=False, unique=True)
    status = Column(String(16), nullable=False, default="active", server_default="active")
    failure_count = Column(Integer, nullable=False, default=0, server_default="0")
    last_success_at = Column(TIMESTAMP, nullable=True)
    last_failure_at = Column(TIMESTAMP, nullable=True)
    last_error_code = Column(String(64), nullable=True)
    last_test_sent_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.current_timestamp())
    updated_at = Column(TIMESTAMP, nullable=True, onupdate=func.current_timestamp())
