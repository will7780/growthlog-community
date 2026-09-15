"""Exercise first-admin storage with synthetic noninteractive input."""
import secrets
from unittest.mock import patch
from app.database import SessionLocal
from app.models import User
from app.auth import verify_password
from scripts.community_init import create_admin

password = secrets.token_urlsafe(24)
with patch("builtins.input", return_value="community_test_bootstrap_admin"), patch("getpass.getpass", return_value=password):
    create_admin()
with SessionLocal() as db:
    user = db.query(User).filter(User.username == "community_test_bootstrap_admin").one()
    assert user.is_admin and user.can_edit_delete_own_entries and verify_password(password, user.password_hash)
    db.delete(user)
    db.commit()
print("COMMUNITY_FIRST_ADMIN_SMOKE_OK")
