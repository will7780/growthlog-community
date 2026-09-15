"""
标签表模型
"""
from sqlalchemy import Column, BigInteger, String, Integer, Boolean, TIMESTAMP, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base


class EntryLabel(Base):
    """标签表模型"""
    __tablename__ = "entry_labels"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(32), nullable=False, unique=True, index=True)
    name = Column(String(64), nullable=False)
    sort_order = Column(Integer, nullable=False, default=0, index=True)
    user_id = Column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)  # NULL=系统预设
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.current_timestamp())

    # 关系
    entries = relationship("Entry", back_populates="label")
    user = relationship("User", foreign_keys=[user_id])
