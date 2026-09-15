"""
Agent pending action model.
"""
from sqlalchemy import BigInteger, Column, Enum, ForeignKey, JSON, String, Text, TIMESTAMP
from sqlalchemy.sql import func

from app.database import Base


class AgentPendingAction(Base):
    """User-confirmed write action proposed by Agent mode."""

    __tablename__ = "agent_pending_actions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id = Column(String(64), nullable=True, index=True)
    action_type = Column(
        Enum(
            "create_todo",
            "create_memory",
            "update_memory",
            "save_summary_entry",
            name="agent_pending_action_type_enum",
        ),
        nullable=False,
    )
    payload_json = Column(JSON, nullable=False)
    status = Column(
        Enum("pending", "confirmed", "rejected", "executed", "failed", name="agent_pending_action_status_enum"),
        nullable=False,
        default="pending",
        index=True,
    )
    result_json = Column(JSON, nullable=True)
    error_message = Column(Text, nullable=True)
    created_by_message_id = Column(BigInteger, nullable=True)
    confirmed_at = Column(TIMESTAMP, nullable=True)
    executed_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.current_timestamp())
    updated_at = Column(TIMESTAMP, nullable=True, onupdate=func.current_timestamp())
