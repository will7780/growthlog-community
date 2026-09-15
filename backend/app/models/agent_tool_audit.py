"""
Agent tool audit model.
"""
from sqlalchemy import BigInteger, Column, Enum, JSON, String, Text, TIMESTAMP, ForeignKey
from sqlalchemy.sql import func

from app.database import Base


class AgentToolAudit(Base):
    """Audit row for every Agent tool execution."""

    __tablename__ = "agent_tool_audits"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id = Column(String(64), nullable=False, index=True)
    tool_name = Column(String(100), nullable=False, index=True)
    permission = Column(Enum("read", "write", "admin", name="agent_permission_enum"), nullable=False, default="read")
    status = Column(Enum("allowed", "denied", "failed", name="agent_tool_status_enum"), nullable=False)
    input_json = Column(JSON, nullable=True)
    output_summary = Column(String(500), nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.current_timestamp())
