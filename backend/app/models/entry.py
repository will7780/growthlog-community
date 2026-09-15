"""
记录表模型
"""
from sqlalchemy import Column, BigInteger, String, Text, TIMESTAMP, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base


class Entry(Base):
    """记录表模型"""
    __tablename__ = "entries"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(
        BigInteger,
        ForeignKey("users.id", ondelete="RESTRICT", onupdate="CASCADE"),
        nullable=False,
        index=True
    )
    label_code = Column(
        String(32),
        ForeignKey("entry_labels.code", ondelete="RESTRICT", onupdate="CASCADE"),
        nullable=False,
        index=True
    )
    parent_id = Column(
        BigInteger,
        ForeignKey("entries.id", ondelete="CASCADE"),
        nullable=True,
        index=True
    )
    content = Column(Text, nullable=False)
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.current_timestamp(), index=True)
    updated_at = Column(TIMESTAMP, nullable=True, onupdate=func.current_timestamp())

    # 关系
    user = relationship("User", back_populates="entries")
    label = relationship("EntryLabel", back_populates="entries")
    parent = relationship("Entry", remote_side=[id], back_populates="children")
    children = relationship("Entry", back_populates="parent", cascade="all, delete-orphan")
    attachments = relationship("EntryAttachment", back_populates="entry", cascade="all, delete-orphan")
