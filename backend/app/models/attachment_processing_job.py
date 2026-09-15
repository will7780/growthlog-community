"""
Attachment processing job model.
"""
from sqlalchemy import BigInteger, Column, Enum, ForeignKey, Integer, JSON, String, Text, TIMESTAMP
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


class AttachmentProcessingJob(Base):
    """Queue job for attachment text extraction and indexing."""

    __tablename__ = "attachment_processing_jobs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(
        BigInteger,
        ForeignKey("users.id", ondelete="RESTRICT", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    attachment_id = Column(
        BigInteger,
        ForeignKey("entry_attachments.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    entry_id = Column(
        BigInteger,
        ForeignKey("entries.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
    )
    job_type = Column(String(50), nullable=False, default="extract_text")
    status = Column(
        Enum(
            "pending",
            "processing",
            "succeeded",
            "failed",
            "cancelled",
            name="attachment_processing_job_status_enum",
        ),
        nullable=False,
        default="pending",
    )
    priority = Column(Integer, nullable=False, default=0)
    attempt_count = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False, default=3)
    locked_by = Column(String(100), nullable=True)
    locked_at = Column(TIMESTAMP, nullable=True, index=True)
    available_at = Column(TIMESTAMP, nullable=False, server_default=func.current_timestamp())
    started_at = Column(TIMESTAMP, nullable=True)
    finished_at = Column(TIMESTAMP, nullable=True)
    error_message = Column(Text, nullable=True)
    metadata_json = Column(JSON, nullable=True)
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.current_timestamp())
    updated_at = Column(TIMESTAMP, nullable=True, onupdate=func.current_timestamp())

    attachment = relationship("EntryAttachment", back_populates="jobs")
