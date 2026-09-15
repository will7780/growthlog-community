"""
AI chat session metadata (persona-bound).
"""
from sqlalchemy import (
    BigInteger,
    Column,
    Enum,
    ForeignKey,
    Integer,
    String,
    TIMESTAMP,
)
from sqlalchemy.sql import func

from app.database import Base


class AIChatSession(Base):
    """Persona-bound chat session metadata."""

    __tablename__ = "ai_chat_sessions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id = Column(String(64), nullable=False)
    persona = Column(
        Enum("retriever", "explainer", "organizer", name="ai_session_persona_enum"),
        nullable=False,
    )
    title = Column(String(255), nullable=True)
    status = Column(
        Enum("active", "archived", name="ai_session_status_enum"),
        nullable=False,
        default="active",
    )
    current_source_set_version = Column(Integer, nullable=True)
    last_message_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.current_timestamp())
    updated_at = Column(TIMESTAMP, nullable=True, onupdate=func.current_timestamp())
