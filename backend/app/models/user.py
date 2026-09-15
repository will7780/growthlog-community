"""
用户表模型
"""
from sqlalchemy import Column, BigInteger, String, Boolean, TIMESTAMP
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base


class User(Base):
    """用户表模型"""
    __tablename__ = "users"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    username = Column(String(64), nullable=False, unique=True, index=True)
    password_hash = Column(String(255), nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    # Requires migration 019; default false so missing privilege cannot escalate.
    is_admin = Column(Boolean, nullable=False, default=False)
    # Requires migration 023; default false — is_admin does NOT imply this privilege.
    can_edit_delete_own_entries = Column(Boolean, nullable=False, default=False, server_default="0")
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.current_timestamp())

    # 关系（可选，用于级联查询）
    entries = relationship("Entry", back_populates="user", cascade="all, delete-orphan")
    todos = relationship("Todo", back_populates="user", cascade="all, delete-orphan")
    todo_daily_plan_items = relationship(
        "TodoDailyPlanItem",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
