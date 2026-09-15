"""
AI 会话表模型
"""
from sqlalchemy import BigInteger, Column, Enum, ForeignKey, Integer, JSON, String, Text, TIMESTAMP, UniqueConstraint
from sqlalchemy.sql import func
from app.database import Base


class AIConversation(Base):
    """AI 会话表模型"""
    __tablename__ = "ai_conversations"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "session_id",
            "proposal_id",
            "role",
            name="uq_ai_conversations_user_session_proposal_role",
        ),
        # R11.3 (029, not yet executed on any database): streaming turn idempotency.
        UniqueConstraint(
            "user_id",
            "session_id",
            "request_id",
            "role",
            name="uq_ai_conversations_user_session_request_role",
        ),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(
        BigInteger,
        nullable=False,
        index=True
    )
    session_id = Column(String(64), nullable=False, index=True)
    role = Column(
        Enum('user', 'assistant', name='role_enum'),
        nullable=False
    )
    content = Column(Text, nullable=False)
    references = Column(JSON, nullable=True)
    source_set_version = Column(Integer, nullable=True)
    proposal_id = Column(
        BigInteger,
        ForeignKey(
            "ai_conversation_source_sets.id",
            ondelete="SET NULL",
            onupdate="CASCADE",
            name="fk_ai_conversations_proposal",
        ),
        nullable=True,
        index=True,
    )
    # R11.3 (029, not yet executed on any database): binds a message to the
    # streaming request that produced it, for idempotent replay/conflict detection.
    request_id = Column(String(64), nullable=True, index=True)
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.current_timestamp())
