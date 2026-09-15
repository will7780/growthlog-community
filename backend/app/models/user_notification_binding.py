"""Historical notification binding ORM retained for migration 025 compatibility."""
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    ForeignKey,
    LargeBinary,
    String,
    TIMESTAMP,
    Time,
)
from sqlalchemy.sql import func

from app.database import Base


class UserNotificationBinding(Base):
    __tablename__ = "user_notification_bindings"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    provider = Column(String(32), nullable=False, default="wxpusher", server_default="wxpusher")
    uid_ciphertext = Column(LargeBinary(512), nullable=False)
    uid_hash = Column(String(64), nullable=False, unique=True)
    status = Column(String(16), nullable=False, default="active", server_default="active")
    reminders_enabled = Column(Boolean, nullable=False, default=False, server_default="0")
    today_plan_time = Column(Time, nullable=True)
    unfinished_time = Column(Time, nullable=True)
    urgent_overdue_enabled = Column(Boolean, nullable=False, default=True, server_default="1")
    quiet_hours_start = Column(Time, nullable=True)
    quiet_hours_end = Column(Time, nullable=True)
    bound_at = Column(TIMESTAMP, nullable=False, server_default=func.current_timestamp())
    last_verified_at = Column(TIMESTAMP, nullable=True)
    last_test_sent_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.current_timestamp())
    updated_at = Column(TIMESTAMP, nullable=True, onupdate=func.current_timestamp())
