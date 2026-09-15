"""
Todo daily plan item — today's arrangement reference (not a second Todo).
"""
from sqlalchemy import (
    BigInteger,
    Column,
    Date,
    ForeignKey,
    String,
    TIMESTAMP,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


class TodoDailyPlanItem(Base):
    __tablename__ = "todo_daily_plan_items"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "todo_id",
            "plan_date",
            name="uq_todo_daily_plan_user_todo_date",
        ),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    todo_id = Column(
        BigInteger,
        ForeignKey("todos.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    plan_date = Column(Date, nullable=False)
    source = Column(String(16), nullable=False, default="manual", server_default="manual")
    status = Column(String(16), nullable=False, default="active", server_default="active")
    carried_from_id = Column(
        BigInteger,
        ForeignKey("todo_daily_plan_items.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.current_timestamp())
    updated_at = Column(TIMESTAMP, nullable=True, onupdate=func.current_timestamp())

    user = relationship("User", back_populates="todo_daily_plan_items")
    todo = relationship("Todo", back_populates="daily_plan_items")
    carried_from = relationship(
        "TodoDailyPlanItem",
        remote_side=[id],
        foreign_keys=[carried_from_id],
        uselist=False,
    )
