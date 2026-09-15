"""Historical binding-session ORM retained for migration 025 compatibility."""
from sqlalchemy import BigInteger, Column, ForeignKey, String, TIMESTAMP
from sqlalchemy.sql import func

from app.database import Base


class NotificationBindingSession(Base):
    __tablename__ = "notification_binding_sessions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    nonce_hash = Column(String(64), nullable=False, unique=True)
    provider_qrcode_code = Column(String(128), nullable=True)
    provider_qrcode_url = Column(String(512), nullable=True)
    last_provider_poll_at = Column(TIMESTAMP, nullable=True)
    expires_at = Column(TIMESTAMP, nullable=False)
    consumed_at = Column(TIMESTAMP, nullable=True)
    status = Column(String(16), nullable=False, default="pending", server_default="pending")
    created_at = Column(TIMESTAMP, nullable=False, server_default=func.current_timestamp())
    updated_at = Column(TIMESTAMP, nullable=True, onupdate=func.current_timestamp())
