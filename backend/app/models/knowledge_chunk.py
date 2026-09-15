"""
Unified knowledge chunk model for RAG.
"""
from sqlalchemy import BigInteger, Column, Enum, ForeignKey, Integer, JSON, String, Text, TIMESTAMP, UniqueConstraint
from sqlalchemy.sql import func

from app.database import Base


class KnowledgeChunk(Base):
    """Searchable text chunk derived from a knowledge source."""

    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        UniqueConstraint("source_id", "chunk_index", name="uk_knowledge_chunk"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    source_id = Column(
        BigInteger,
        ForeignKey("knowledge_sources.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(BigInteger, nullable=False, index=True)
    origin_type = Column(
        Enum("entry", "attachment", "memory", "todo", "notion_page", name="knowledge_chunk_origin_type"),
        nullable=False,
    )
    origin_id = Column(BigInteger, nullable=False, index=True)
    entry_id = Column(BigInteger, nullable=True, index=True)
    chunk_index = Column(Integer, nullable=False)
    chunk_type = Column(
        Enum("entry_text", "pdf_text", "ppt_text", "ocr", "caption", "memory", "notion_text", name="knowledge_chunk_type"),
        nullable=False,
        default="entry_text",
    )
    title = Column(String(255), nullable=False, default="")
    content = Column(Text, nullable=False)
    page_no = Column(Integer, nullable=True)
    slide_no = Column(Integer, nullable=True)
    metadata_json = Column(JSON, nullable=True)
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.current_timestamp())
    updated_at = Column(TIMESTAMP, nullable=True, onupdate=func.current_timestamp())
