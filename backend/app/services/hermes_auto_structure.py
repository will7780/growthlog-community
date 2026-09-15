"""
Hermes auto-structure boundary.
"""
from app.services.feature_gate import feature_not_enabled


def get_auto_structure_status(db, user_id: int) -> dict:
    return {
        "enabled": False,
        "provider": "hermes",
        "poll_interval_seconds": 0,
        "max_records_per_run": 0,
        "max_tokens_per_chunk": 0,
        "last_processed_at": None,
        "pending_records": 0,
        "status": "disabled",
        "message": "Hermes 自动结构化尚未启用。",
    }


def run_hermes_auto_structure(*args, **kwargs):
    feature_not_enabled("Hermes 自动结构化")
