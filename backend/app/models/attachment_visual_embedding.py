"""
Attachment visual embedding model.

Reserved for future OpenCLIP/ColPali-style visual retrieval. Vectors in this
table must never be mixed with E5 text embeddings in knowledge_embeddings or
attachment_embeddings.
"""
from sqlalchemy import BigInteger, Column, ForeignKey, Integer, JSON, String, TIMESTAMP
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


class AttachmentVisualEmbedding(Base):
    """Visual embedding vector for an attachment image or page."""

    __tablename__ = "attachment_visual_embeddings"

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
        index=True,
    )
    image_ref = Column(String(500), nullable=True)
    page_no = Column(Integer, nullable=True)
    provider = Column(String(100), nullable=False, default="disabled")
    model = Column(String(100), nullable=False, default="disabled")
    embedding_dim = Column(Integer, nullable=False, default=0)
    vector_json = Column(JSON, nullable=True)
    metadata_json = Column(JSON, nullable=True)
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.current_timestamp())
    updated_at = Column(TIMESTAMP, nullable=True, onupdate=func.current_timestamp())

    attachment = relationship("EntryAttachment", back_populates="visual_embeddings")
