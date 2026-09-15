"""
Unified knowledge source model for RAG.
"""
from sqlalchemy import BigInteger, Column, Enum, JSON, String, TIMESTAMP, UniqueConstraint
from sqlalchemy.sql import func

from app.database import Base


class KnowledgeSource(Base):
    """A user-owned source that can produce searchable chunks."""

    __tablename__ = "knowledge_sources"
    __table_args__ = (
        UniqueConstraint("user_id", "source_type", "source_id", name="uk_knowledge_source"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=False, index=True)
    source_type = Column(
        Enum("entry", "attachment", "memory", "todo", "notion_page", name="knowledge_source_type"),
        nullable=False,
    )
    source_id = Column(BigInteger, nullable=False)
    title = Column(String(255), nullable=False, default="")
    status = Column(
        Enum("pending", "indexed", "failed", name="knowledge_source_status"),
        nullable=False,
        default="pending",
    )
    metadata_json = Column(JSON, nullable=True)
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.current_timestamp())
    updated_at = Column(TIMESTAMP, nullable=True, onupdate=func.current_timestamp())
