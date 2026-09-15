"""
Attachment embedding model.
"""
from sqlalchemy import BigInteger, Column, ForeignKey, JSON, String, TIMESTAMP
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


class AttachmentEmbedding(Base):
    """Embedding vector for an attachment chunk."""

    __tablename__ = "attachment_embeddings"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    chunk_id = Column(
        BigInteger,
        ForeignKey("attachment_chunks.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    user_id = Column(
        BigInteger,
        ForeignKey("users.id", ondelete="RESTRICT", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    embedding_model = Column(String(100), nullable=False)
    vector = Column(JSON, nullable=False)
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.current_timestamp())

    chunk = relationship("AttachmentChunk", back_populates="embedding")
