"""
Agent long-term memory model.
"""
from sqlalchemy import BigInteger, Boolean, Column, Enum, Float, String, Text, TIMESTAMP, ForeignKey
from sqlalchemy.sql import func

from app.database import Base


class AgentMemory(Base):
    """User-scoped long-term memory used by Agent mode."""

    __tablename__ = "agent_memories"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    memory_type = Column(
        Enum("goal", "preference", "project", "profile", "insight", name="agent_memory_type_enum"),
        nullable=False,
    )
    content = Column(Text, nullable=False)
    source = Column(String(100), nullable=False, default="agent")
    confidence = Column(Float, nullable=False, default=0.6)
    is_active = Column(Boolean, nullable=False, default=True, index=True)
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.current_timestamp())
    updated_at = Column(TIMESTAMP, nullable=True, onupdate=func.current_timestamp())
