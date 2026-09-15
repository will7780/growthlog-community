"""小要事表模型。"""
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    TIMESTAMP,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


class Todo(Base):
    """小要事表模型。"""

    __tablename__ = "todos"
    __table_args__ = (
        UniqueConstraint("id", "user_id", name="uq_todos_id_user"),
        ForeignKeyConstraint(
            ["parent_id", "user_id"],
            ["todos.id", "todos.user_id"],
            name="fk_todos_parent_owner",
            ondelete="CASCADE",
        ),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    parent_id = Column(BigInteger, nullable=True, index=True)
    sort_order = Column(Integer, nullable=True)
    content = Column(String(500), nullable=False)
    priority = Column(String(2), nullable=False, default="P4", server_default="P4")
    due_date = Column(Date, nullable=True)
    is_done = Column(Boolean, nullable=False, default=False)
    is_urgent = Column(Boolean, nullable=False, default=False, server_default="0")
    completed_at = Column(TIMESTAMP, nullable=True)
    completion_note = Column(String(1000), nullable=True)
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.current_timestamp())
    updated_at = Column(TIMESTAMP, nullable=True, onupdate=func.current_timestamp())

    user = relationship("User", back_populates="todos", foreign_keys=[user_id])
    daily_plan_items = relationship(
        "TodoDailyPlanItem",
        back_populates="todo",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )