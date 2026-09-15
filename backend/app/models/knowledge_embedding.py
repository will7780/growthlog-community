"""
Unified knowledge chunk embedding model.
"""
from sqlalchemy import BigInteger, Column, ForeignKey, JSON, String, TIMESTAMP, UniqueConstraint
from sqlalchemy.sql import func

from app.database import Base


class KnowledgeEmbedding(Base):
    """Embedding vector for a unified knowledge chunk."""

    __tablename__ = "knowledge_embeddings"
    __table_args__ = (
        UniqueConstraint("chunk_id", "embedding_model", name="uk_knowledge_embedding"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    chunk_id = Column(
        BigInteger,
        ForeignKey("knowledge_chunks.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(BigInteger, nullable=False, index=True)
    embedding_model = Column(String(100), nullable=False)
    vector = Column(JSON, nullable=False)
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.current_timestamp())
    updated_at = Column(TIMESTAMP, nullable=True, onupdate=func.current_timestamp())
