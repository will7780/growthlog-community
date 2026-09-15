"""
向量嵌入表模型
"""
from sqlalchemy import Column, BigInteger, String, Enum, JSON, TIMESTAMP
from sqlalchemy.sql import func
from app.database import Base


class Embedding(Base):
    """向量嵌入表模型"""
    __tablename__ = "embeddings"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    entry_id = Column(
        BigInteger,
        nullable=False,
        index=True
    )
    entry_type = Column(
        Enum('main', 'child', name='entry_type_enum'),
        nullable=False,
        default='main'
    )
    title_vector = Column(JSON, nullable=True)
    content_vector = Column(JSON, nullable=True)
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.current_timestamp())
    updated_at = Column(TIMESTAMP, nullable=True, onupdate=func.current_timestamp())
