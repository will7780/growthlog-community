"""One-time Notion OAuth state hashes."""
from sqlalchemy import BigInteger, Column, String, TIMESTAMP
from sqlalchemy.sql import func

from app.database import Base


class NotionOAuthState(Base):
    __tablename__ = "notion_oauth_states"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=False, index=True)
    state_hash = Column(String(64), nullable=False, unique=True)
    expires_at = Column(TIMESTAMP, nullable=False)
    consumed_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.current_timestamp())
