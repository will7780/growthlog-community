"""
Versioned explainer source sets for AI chat sessions.
"""
from sqlalchemy import (
    BigInteger,
    Column,
    Enum,
    ForeignKey,
    Integer,
    JSON,
    String,
    TIMESTAMP,
)
from sqlalchemy.sql import func

from app.database import Base


class AIConversationSourceSet(Base):
    """Immutable versioned source manifest for explainer sessions."""

    __tablename__ = "ai_conversation_source_sets"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id = Column(String(64), nullable=False, index=True)
    version = Column(Integer, nullable=True)
    base_version = Column(Integer, nullable=True)
    status = Column(
        Enum(
            "proposed",
            "locked",
            "superseded",
            "stale",
            name="ai_source_set_status_enum",
        ),
        nullable=False,
        default="proposed",
    )
    source_manifest = Column(JSON, nullable=False)
    content_fingerprint = Column(String(64), nullable=True)
    locked_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.current_timestamp())
    updated_at = Column(TIMESTAMP, nullable=True, onupdate=func.current_timestamp())
