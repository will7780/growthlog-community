"""Local Notion page mirror. Notion remains the business source of truth."""
from sqlalchemy import BigInteger, Column, Enum, ForeignKey, Integer, String, TIMESTAMP, Text, UniqueConstraint
from sqlalchemy.sql import func

from app.database import Base


class NotionPage(Base):
    __tablename__ = "notion_pages"
    __table_args__ = (
        UniqueConstraint("connection_id", "notion_page_uuid", name="uk_notion_pages_connection_uuid"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    connection_id = Column(
        BigInteger,
        ForeignKey("notion_connections.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(BigInteger, nullable=False, index=True)
    notion_page_uuid = Column(String(36), nullable=False)
    parent_notion_page_uuid = Column(String(36), nullable=True)
    title = Column(String(512), nullable=False, default="")
    breadcrumb = Column(String(1024), nullable=False, default="")
    notion_url = Column(String(512), nullable=True)
    normalized_text = Column(Text, nullable=True)
    remote_last_edited_at = Column(TIMESTAMP, nullable=True)
    observed_content_hash = Column(String(64), nullable=False, default="")
    indexed_content_hash = Column(String(64), nullable=True)
    sync_status = Column(
        Enum(
            "pending",
            "processing",
            "indexed",
            "partial",
            "failed",
            "permission_lost",
            "deleted",
            name="notion_page_sync_status",
        ),
        nullable=False,
        default="pending",
    )
    unsupported_block_count = Column(Integer, nullable=False, default=0)
    last_error_code = Column(String(64), nullable=True)
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.current_timestamp())
    updated_at = Column(TIMESTAMP, nullable=True, onupdate=func.current_timestamp())
