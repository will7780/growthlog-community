"""
Entry attachment model.
"""
from sqlalchemy import BigInteger, Column, Enum, ForeignKey, Integer, String, TIMESTAMP, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


class EntryAttachment(Base):
    """Files uploaded for an entry."""

    __tablename__ = "entry_attachments"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    entry_id = Column(
        BigInteger,
        ForeignKey("entries.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(
        BigInteger,
        ForeignKey("users.id", ondelete="RESTRICT", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    original_filename = Column(String(255), nullable=False)
    storage_filename = Column(String(255), nullable=False)
    # Legacy column kept by migration 008 for older schemas. New inserts must
    # populate it when the physical column still exists as NOT NULL.
    file_name = Column(String(255), nullable=True)
    mime_type = Column(String(100), nullable=False)
    file_ext = Column(String(20), nullable=False)
    file_size = Column(BigInteger, nullable=False)
    content_hash = Column(String(64), nullable=False)
    storage_path = Column(String(500), nullable=False)
    status = Column(
        Enum("uploaded", "processing", "indexed", "failed", name="attachment_status"),
        nullable=False,
        default="uploaded",
    )
    sort_order = Column(Integer, nullable=True)
    page_count = Column(Integer, nullable=True)
    slide_count = Column(Integer, nullable=True)
    preview_path = Column(String(500), nullable=True)
    error_message = Column(Text, nullable=True)
    processed_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.current_timestamp())
    updated_at = Column(TIMESTAMP, nullable=True, onupdate=func.current_timestamp())

    entry = relationship("Entry", back_populates="attachments")
    user = relationship("User")
    chunks = relationship("AttachmentChunk", back_populates="attachment", cascade="all, delete-orphan")
    visual_embeddings = relationship(
        "AttachmentVisualEmbedding",
        back_populates="attachment",
        cascade="all, delete-orphan",
        # Avoid SELECT on attachment_visual_embeddings when migration 013 is absent.
        passive_deletes=True,
    )
    jobs = relationship(
        "AttachmentProcessingJob",
        back_populates="attachment",
        cascade="all, delete-orphan",
    )
