"""Notion OAuth connection for one GrowthLog user."""
from sqlalchemy import BigInteger, Column, Enum, Integer, LargeBinary, String, TIMESTAMP
from sqlalchemy.sql import func

from app.database import Base


class NotionConnection(Base):
    __tablename__ = "notion_connections"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=False, unique=True, index=True)
    bot_id = Column(String(64), nullable=False, unique=True)
    workspace_id = Column(String(64), nullable=False)
    workspace_name = Column(String(255), nullable=False, default="")
    owner_notion_user_id = Column(String(64), nullable=True)
    access_token_encrypted = Column(LargeBinary, nullable=False)
    refresh_token_encrypted = Column(LargeBinary, nullable=True)
    token_crypto_version = Column(Integer, nullable=False, default=1)
    status = Column(
        Enum("active", "reauth_required", "disconnected", name="notion_connection_status"),
        nullable=False,
        default="active",
    )
    sync_status = Column(
        Enum("pending", "syncing", "ready", "partial", "failed", name="notion_connection_sync_status"),
        nullable=False,
        default="pending",
    )
    last_sync_started_at = Column(TIMESTAMP, nullable=True)
    last_sync_completed_at = Column(TIMESTAMP, nullable=True)
    last_reconcile_at = Column(TIMESTAMP, nullable=True)
    last_error_code = Column(String(64), nullable=True)
    token_expires_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.current_timestamp())
    updated_at = Column(TIMESTAMP, nullable=True, onupdate=func.current_timestamp())
