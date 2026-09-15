"""
Scheduled review service boundary.
"""
from app.services.feature_gate import feature_not_enabled


def get_scheduled_review_status(db, user_id: int) -> dict:
    return {
        "system_enabled": False,
        "user_authorized": False,
        "effective_enabled": False,
        "enabled": False,
        "provider": "hermes",
        "review_days": 7,
        "poll_interval_seconds": 0,
        "scheduled_review_last_at": None,
        "pending_count": 0,
        "pending_entries": 0,
        "draft_count": 0,
        "pending_drafts": 0,
        "last_run_at": None,
        "last_error_code": None,
        "status": "disabled",
        "message": "自动复盘尚未启用。",
    }


def run_scheduled_review(*args, **kwargs):
    feature_not_enabled("自动复盘")
