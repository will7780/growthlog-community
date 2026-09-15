"""
AI 派生内容表模型（整合/复盘等，不污染原始 Entry）
"""
from sqlalchemy import Column, BigInteger, String, Text, JSON, TIMESTAMP, ForeignKey, UniqueConstraint
from sqlalchemy.sql import func

from app.database import Base


class AIDerivedContent(Base):
    __tablename__ = "ai_derived_contents"
    __table_args__ = (
        UniqueConstraint("user_id", "confirm_key", name="uk_derived_user_confirm_key"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    type = Column(String(32), nullable=False, index=True)
    title = Column(String(255), nullable=False)
    content = Column(Text, nullable=False)
    scope_type = Column(String(32), nullable=True)
    source_entry_ids = Column(JSON, nullable=False)
    meta = Column("metadata", JSON, nullable=True)
    status = Column(String(32), nullable=False, default="confirmed")
    confirm_key = Column(String(64), nullable=True)
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.current_timestamp())
    updated_at = Column(TIMESTAMP, nullable=True, onupdate=func.current_timestamp())
