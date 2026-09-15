"""Per-user notification preferences (migration 026)."""
from sqlalchemy import BigInteger, Boolean, Column, ForeignKey, TIMESTAMP, Time
from sqlalchemy.sql import func

from app.database import Base


class NotificationPreference(Base):
    __tablename__ = "notification_preferences"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    reminders_enabled = Column(Boolean, nullable=False, default=False, server_default="0")
    today_plan_time = Column(Time, nullable=True)
    unfinished_time = Column(Time, nullable=True)
    urgent_overdue_enabled = Column(Boolean, nullable=False, default=True, server_default="1")
    quiet_hours_start = Column(Time, nullable=True)
    quiet_hours_end = Column(Time, nullable=True)
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.current_timestamp())
    updated_at = Column(TIMESTAMP, nullable=True, onupdate=func.current_timestamp())
