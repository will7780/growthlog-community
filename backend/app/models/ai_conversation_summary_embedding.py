"""
AI conversation summary embedding model.

Dedicated table for cross-session summary retrieval; separate from message-level
ai_conversation_embeddings and all entry/knowledge/attachment indexes.
"""
from sqlalchemy import BigInteger, Column, ForeignKey, JSON, String, TIMESTAMP
from sqlalchemy.sql import func

from app.database import Base


class AIConversationSummaryEmbedding(Base):
    """Text embedding for a compressed AI conversation summary."""

    __tablename__ = "ai_conversation_summary_embeddings"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    summary_id = Column(
        BigInteger,
        ForeignKey("ai_conversation_summaries.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id = Column(String(64), nullable=False, index=True)
    summary_hash = Column(String(64), nullable=False)
    embedding_model = Column(String(100), nullable=False)
    vector = Column(JSON, nullable=False)
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.current_timestamp())
    updated_at = Column(TIMESTAMP, nullable=True, onupdate=func.current_timestamp())
