"""
Attachment text chunk model.
"""
from sqlalchemy import BigInteger, Column, Enum, ForeignKey, Integer, JSON, Text, TIMESTAMP, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


class AttachmentChunk(Base):
    """Extracted text chunk from an attachment."""

    __tablename__ = "attachment_chunks"
    __table_args__ = (
        UniqueConstraint("attachment_id", "chunk_index", name="uk_attachment_chunk"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
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
        index=True,
    )
    user_id = Column(
        BigInteger,
        ForeignKey("users.id", ondelete="RESTRICT", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_index = Column(Integer, nullable=False)
    modality = Column(
        Enum("pdf_text", "ppt_text", "ocr", "caption", "table", name="attachment_chunk_modality"),
        nullable=False,
    )
    page_no = Column(Integer, nullable=True)
    slide_no = Column(Integer, nullable=True)
    bbox_json = Column(JSON, nullable=True)
    content = Column(Text, nullable=False)
    token_count = Column(Integer, nullable=True)
    metadata_json = Column(JSON, nullable=True)
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.current_timestamp())

    attachment = relationship("EntryAttachment", back_populates="chunks")
    embedding = relationship("AttachmentEmbedding", back_populates="chunk", uselist=False, cascade="all, delete-orphan")
