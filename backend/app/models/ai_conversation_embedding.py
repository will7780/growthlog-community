"""
AI conversation message embedding model.

Dedicated table for ai_session_search vector retrieval; not mixed into
entries/knowledge/attachment embedding tables.
"""
from sqlalchemy import BigInteger, Column, Enum, ForeignKey, JSON, String, TIMESTAMP
from sqlalchemy.sql import func

from app.database import Base


class AIConversationEmbedding(Base):
    """Text embedding for a single AI conversation message."""

    __tablename__ = "ai_conversation_embeddings"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    conversation_id = Column(
        BigInteger,
        ForeignKey("ai_conversations.id", ondelete="CASCADE", onupdate="CASCADE"),
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
    role = Column(
        Enum("user", "assistant", name="ai_conv_emb_role_enum"),
        nullable=False,
    )
    content_hash = Column(String(64), nullable=False)
    embedding_model = Column(String(100), nullable=False)
    vector = Column(JSON, nullable=False)
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.current_timestamp())
    updated_at = Column(TIMESTAMP, nullable=True, onupdate=func.current_timestamp())
