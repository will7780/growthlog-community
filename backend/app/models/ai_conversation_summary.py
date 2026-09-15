"""
AI conversation summary model.
"""
from sqlalchemy import BigInteger, Column, Integer, JSON, String, Text, TIMESTAMP, ForeignKey
from sqlalchemy.sql import func

from app.database import Base


class AIConversationSummary(Base):
    """Compressed summary for a chat session."""

    __tablename__ = "ai_conversation_summaries"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id = Column(String(64), nullable=False, index=True)
    summary = Column(Text, nullable=False)
    structured_json = Column(JSON, nullable=True)
    message_count = Column(Integer, nullable=False, default=0)
    last_message_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.current_timestamp())
    updated_at = Column(TIMESTAMP, nullable=True, onupdate=func.current_timestamp())
