"""
Agent long-term memory embedding model.

Dedicated table for memory vector recall; not mixed into
entries/knowledge/attachment/ai_conversation embedding tables.
"""
from sqlalchemy import BigInteger, Column, Enum, ForeignKey, JSON, String, TIMESTAMP
from sqlalchemy.sql import func

from app.database import Base


class AgentMemoryEmbedding(Base):
    """Text embedding for a single active agent memory."""

    __tablename__ = "agent_memory_embeddings"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    memory_id = Column(
        BigInteger,
        ForeignKey("agent_memories.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    memory_type = Column(
        Enum(
            "goal",
            "preference",
            "project",
            "profile",
            "insight",
            name="agent_memory_emb_type_enum",
        ),
        nullable=False,
    )
    content_hash = Column(String(64), nullable=False)
    embedding_model = Column(String(100), nullable=False)
    vector = Column(JSON, nullable=False)
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.current_timestamp())
    updated_at = Column(TIMESTAMP, nullable=True, onupdate=func.current_timestamp())
