"""Provider-neutral notification preferences (migration 026)."""
from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import NotificationPreference
from app.timeutil import now_local

_PREFS_TABLE_READY: Optional[bool] = None
_WEB_PUSH_TABLE_READY: Optional[bool] = None


class PreferenceError(Exception):
    def __init__(self, code: str, status_code: int = 400):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


def reset_schema_capability_cache() -> None:
    """Test helper: clear cached INFORMATION_SCHEMA probes."""
    global _PREFS_TABLE_READY, _WEB_PUSH_TABLE_READY
    _PREFS_TABLE_READY = None
    _WEB_PUSH_TABLE_READY = None


def _table_exists(db: Session, table: str) -> bool:
    return bool(
        db.execute(
            text(
                "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t"
            ),
            {"t": table},
        ).scalar()
    )


def preferences_table_ready(db: Session) -> bool:
    global _PREFS_TABLE_READY
    if _PREFS_TABLE_READY is None:
        _PREFS_TABLE_READY = _table_exists(db, "notification_preferences")
    return bool(_PREFS_TABLE_READY)


def web_push_table_ready(db: Session) -> bool:
    global _WEB_PUSH_TABLE_READY
    if _WEB_PUSH_TABLE_READY is None:
        _WEB_PUSH_TABLE_READY = _table_exists(db, "web_push_subscriptions")
    return bool(_WEB_PUSH_TABLE_READY)


def _fmt_time(value: Optional[time]) -> Optional[str]:
    if value is None:
        return None
    return value.strftime("%H:%M")


def _parse_hhmm(value: Optional[str], field: str) -> Optional[time]:
    if value is None or value == "":
        return None
    try:
        hh, mm = value.split(":")
        return time(hour=int(hh), minute=int(mm))
    except Exception as exc:  # noqa: BLE001
        raise PreferenceError(f"INVALID_{field.upper()}") from exc


def get_or_create_preferences(db: Session, user_id: int) -> NotificationPreference:
    if not preferences_table_ready(db):
        raise PreferenceError("PREFERENCES_SCHEMA_MISSING", 503)
    pref = (
        db.query(NotificationPreference)
        .filter(NotificationPreference.user_id == user_id)
        .first()
    )
    if pref:
        return pref
    pref = NotificationPreference(
        user_id=user_id,
        reminders_enabled=False,
        today_plan_time=time(9, 0),
        unfinished_time=time(20, 0),
        urgent_overdue_enabled=True,
    )
    db.add(pref)
    db.commit()
    db.refresh(pref)
    return pref


def preferences_as_dict(pref: NotificationPreference) -> dict[str, Any]:
    return {
        "reminders_enabled": bool(pref.reminders_enabled),
        "today_plan_time": _fmt_time(pref.today_plan_time) or "09:00",
        "unfinished_time": _fmt_time(pref.unfinished_time) or "20:00",
        "urgent_overdue_enabled": bool(pref.urgent_overdue_enabled),
        "quiet_hours_start": _fmt_time(pref.quiet_hours_start),
        "quiet_hours_end": _fmt_time(pref.quiet_hours_end),
    }


def patch_preferences(db: Session, user_id: int, patch: dict[str, Any]) -> NotificationPreference:
    from app.services.notification_service import cancel_pending_outbox

    pref = get_or_create_preferences(db, user_id)
    if "reminders_enabled" in patch and patch["reminders_enabled"] is not None:
        pref.reminders_enabled = bool(patch["reminders_enabled"])
        if not pref.reminders_enabled:
            cancel_pending_outbox(db, user_id)
    if "today_plan_time" in patch:
        pref.today_plan_time = _parse_hhmm(patch.get("today_plan_time"), "today_plan_time") or time(9, 0)
    if "unfinished_time" in patch:
        pref.unfinished_time = _parse_hhmm(patch.get("unfinished_time"), "unfinished_time") or time(20, 0)
    if "urgent_overdue_enabled" in patch and patch["urgent_overdue_enabled"] is not None:
        pref.urgent_overdue_enabled = bool(patch["urgent_overdue_enabled"])
    if "quiet_hours_start" in patch:
        pref.quiet_hours_start = _parse_hhmm(patch.get("quiet_hours_start"), "quiet_hours_start")
    if "quiet_hours_end" in patch:
        pref.quiet_hours_end = _parse_hhmm(patch.get("quiet_hours_end"), "quiet_hours_end")
    pref.updated_at = now_local()
    db.commit()
    db.refresh(pref)
    return pref


def in_quiet_hours(pref: NotificationPreference, when: datetime) -> bool:
    start = pref.quiet_hours_start
    end = pref.quiet_hours_end
    if start is None or end is None:
        return False
    t = when.time().replace(second=0, microsecond=0)
    if start == end:
        return False
    if start < end:
        return start <= t < end
    return t >= start or t < end


def next_quiet_end(pref: NotificationPreference, when: datetime) -> datetime:
    end = pref.quiet_hours_end
    assert end is not None
    candidate = datetime.combine(when.date(), end)
    if candidate <= when:
        candidate += timedelta(days=1)
    return candidate
