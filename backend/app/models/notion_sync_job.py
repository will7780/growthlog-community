"""Notion sync job with independent claim/lease from attachment jobs."""
from sqlalchemy import BigInteger, Column, Enum, ForeignKey, Integer, String, TIMESTAMP, UniqueConstraint
from sqlalchemy.sql import func

from app.database import Base


class NotionSyncJob(Base):
    __tablename__ = "notion_sync_jobs"
    __table_args__ = (
        UniqueConstraint("provider_event_id", name="uk_notion_sync_jobs_event"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=False, index=True)
    connection_id = Column(
        BigInteger,
        ForeignKey("notion_connections.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    notion_page_id = Column(BigInteger, nullable=True)
    job_type = Column(
        Enum(
            "initial_discovery",
            "reconcile",
            "sync_page",
            "delete_page",
            name="notion_sync_job_type",
        ),
        nullable=False,
    )
    status = Column(
        Enum(
            "pending",
            "processing",
            "retry",
            "succeeded",
            "failed",
            "cancelled",
            name="notion_sync_job_status",
        ),
        nullable=False,
        default="pending",
    )
    provider_event_id = Column(String(128), nullable=True)
    attempt_count = Column(Integer, nullable=False, default=0)
    available_at = Column(TIMESTAMP, nullable=False, server_default=func.current_timestamp())
    locked_at = Column(TIMESTAMP, nullable=True)
    lock_owner = Column(String(100), nullable=True)
    lease_until = Column(TIMESTAMP, nullable=True)
    last_error_code = Column(String(64), nullable=True)
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.current_timestamp())
    updated_at = Column(TIMESTAMP, nullable=True, onupdate=func.current_timestamp())
