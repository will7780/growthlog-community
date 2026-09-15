"""
Scheduled review authorization boundary.
"""
from app.services.feature_gate import feature_not_enabled


def assert_user_hermes_review_authorized(db, user) -> None:
    feature_not_enabled("自动复盘授权")
