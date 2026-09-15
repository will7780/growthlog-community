"""
Phase R7.1 Today's Plan — real SQLAlchemy service/route tests (in-memory SQLite).

No local/production business DB writes. Migration 022 is not executed against MySQL.
"""
from __future__ import annotations

import sys
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.database import Base
from app.models import Todo, TodoDailyPlanItem, User
from app.routes import todos as todos_routes
from app.services import todo_daily_plan as plan_svc


TODAY = date(2026, 7, 24)
_SEQ = {"user": 0, "todo": 0, "plan": 0}


@contextmanager
def memory_db() -> Iterator[Session]:
    _SEQ["user"] = 0
    _SEQ["todo"] = 0
    _SEQ["plan"] = 0
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _connection_record):  # noqa: ANN001
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(
        engine,
        tables=[User.__table__, Todo.__table__, TodoDailyPlanItem.__table__],
    )
    SessionLocal = sessionmaker(bind=engine)

    @event.listens_for(SessionLocal, "before_flush")
    def _assign_missing_ids(session, _ctx, _instances):  # noqa: ANN001
        for obj in session.new:
            if isinstance(obj, User) and getattr(obj, "id", None) is None:
                obj.id = _next_id("user")
            elif isinstance(obj, Todo) and getattr(obj, "id", None) is None:
                obj.id = _next_id("todo")
            elif isinstance(obj, TodoDailyPlanItem) and getattr(obj, "id", None) is None:
                obj.id = _next_id("plan")

    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
        engine.dispose()


def _next_id(kind: str) -> int:
    _SEQ[kind] += 1
    return _SEQ[kind]


def _user(db: Session, username: str = "u1") -> User:
    row = User(
        id=_next_id("user"),
        username=username,
        password_hash="x",
        is_active=True,
        is_admin=False,
    )
    db.add(row)
    db.flush()
    return row


def _todo(
    db: Session,
    user: User,
    *,
    content: str = "t",
    urgent: bool = False,
    due: date | None = None,
    done: bool = False,
) -> Todo:
    row = Todo(
        id=_next_id("todo"),
        user_id=user.id,
        content=content,
        priority="P4",
        due_date=due,
        is_done=done,
        is_urgent=urgent,
    )
    db.add(row)
    db.flush()
    return row


def _plan(
    db: Session,
    user: User,
    todo: Todo,
    plan_date: date,
    *,
    status: str = "active",
    source: str = "manual",
    carried_from_id: int | None = None,
) -> TodoDailyPlanItem:
    row = TodoDailyPlanItem(
        id=_next_id("plan"),
        user_id=user.id,
        todo_id=todo.id,
        plan_date=plan_date,
        source=source,
        status=status,
        carried_from_id=carried_from_id,
    )
    db.add(row)
    db.flush()
    return row


def test_stale_preview_plan_date_rejects_without_write():
    with memory_db() as db, patch.object(plan_svc, "today_local", return_value=TODAY):
        user = _user(db)
        todo = _todo(db, user, urgent=True, due=TODAY - timedelta(days=1))
        before = db.query(TodoDailyPlanItem).count()
        try:
            plan_svc.confirm_urgent_decisions(
                db,
                user_id=user.id,
                plan_date=TODAY - timedelta(days=1),
                decisions=[(todo.id, "keep")],
            )
            raise AssertionError("expected ReviewStaleError")
        except plan_svc.ReviewStaleError as exc:
            assert exc.code == "REVIEW_STALE"
        assert db.query(TodoDailyPlanItem).count() == before


def test_urgent_rejects_non_candidate_and_cross_user():
    with memory_db() as db, patch.object(plan_svc, "today_local", return_value=TODAY):
        owner = _user(db, "owner")
        other = _user(db, "other")
        normal = _todo(db, owner, content="normal", urgent=False)
        foreign = _todo(db, other, content="foreign", urgent=True, due=TODAY - timedelta(days=2))
        try:
            plan_svc.confirm_urgent_decisions(
                db,
                user_id=owner.id,
                plan_date=TODAY,
                decisions=[(normal.id, "delete")],
            )
            raise AssertionError("expected reject non-candidate")
        except plan_svc.ReviewRejectedError:
            pass
        assert db.query(Todo).filter(Todo.id == normal.id).first() is not None
        try:
            plan_svc.confirm_urgent_decisions(
                db,
                user_id=owner.id,
                plan_date=TODAY,
                decisions=[(foreign.id, "delete")],
            )
            raise AssertionError("expected reject cross-user")
        except plan_svc.ReviewRejectedError:
            pass
        assert db.query(Todo).filter(Todo.id == foreign.id).first() is not None


def test_duplicate_and_conflicting_decisions_rejected():
    with memory_db() as db, patch.object(plan_svc, "today_local", return_value=TODAY):
        user = _user(db)
        todo = _todo(db, user, urgent=True, due=TODAY - timedelta(days=1))
        try:
            plan_svc.confirm_urgent_decisions(
                db,
                user_id=user.id,
                plan_date=TODAY,
                decisions=[(todo.id, "keep"), (todo.id, "keep")],
            )
            raise AssertionError("duplicate should reject")
        except plan_svc.ReviewRejectedError:
            pass
        try:
            plan_svc.confirm_urgent_decisions(
                db,
                user_id=user.id,
                plan_date=TODAY,
                decisions=[(todo.id, "keep"), (todo.id, "delete")],
            )
            raise AssertionError("conflict should reject")
        except plan_svc.ReviewRejectedError:
            pass


def test_deleted_todo_delete_is_idempotent_noop():
    with memory_db() as db, patch.object(plan_svc, "today_local", return_value=TODAY):
        user = _user(db)
        kept = _todo(db, user, content="keep-me", urgent=True, due=TODAY - timedelta(days=1))
        ghost_id = 999999
        kept_ids, deleted_ids = plan_svc.confirm_urgent_decisions(
            db,
            user_id=user.id,
            plan_date=TODAY,
            decisions=[(kept.id, "keep"), (ghost_id, "delete")],
        )
        assert kept.id in kept_ids
        assert ghost_id in deleted_ids
        assert db.query(Todo).filter(Todo.id == kept.id).first() is not None
        today_item = (
            db.query(TodoDailyPlanItem)
            .filter(
                TodoDailyPlanItem.todo_id == kept.id,
                TodoDailyPlanItem.plan_date == TODAY,
            )
            .first()
        )
        assert today_item is not None and today_item.status == "active"


def test_rollover_only_processes_reviewed_ids():
    with memory_db() as db, patch.object(plan_svc, "today_local", return_value=TODAY):
        user = _user(db)
        reviewed = _todo(db, user, content="reviewed")
        fresh = _todo(db, user, content="new-after-preview")
        _plan(db, user, reviewed, TODAY - timedelta(days=1))
        _plan(db, user, fresh, TODAY - timedelta(days=1))
        added, returned = plan_svc.confirm_rollover(
            db,
            user_id=user.id,
            plan_date=TODAY,
            reviewed_todo_ids=[reviewed.id],
            selected_todo_ids=[reviewed.id],
        )
        assert reviewed.id in added
        assert fresh.id not in added
        fresh_past = (
            db.query(TodoDailyPlanItem)
            .filter(
                TodoDailyPlanItem.todo_id == fresh.id,
                TodoDailyPlanItem.plan_date == TODAY - timedelta(days=1),
            )
            .first()
        )
        assert fresh_past is not None and fresh_past.status == "active"
        fresh_today = (
            db.query(TodoDailyPlanItem)
            .filter(
                TodoDailyPlanItem.todo_id == fresh.id,
                TodoDailyPlanItem.plan_date == TODAY,
            )
            .first()
        )
        assert fresh_today is None
        assert returned == []


def test_lineage_points_to_latest_past_active():
    with memory_db() as db, patch.object(plan_svc, "today_local", return_value=TODAY):
        user = _user(db)
        todo = _todo(db, user, content="carry")
        older = _plan(db, user, todo, TODAY - timedelta(days=3))
        newer = _plan(db, user, todo, TODAY - timedelta(days=1))
        item = plan_svc.add_todo_to_today(
            db,
            user_id=user.id,
            todo_id=todo.id,
            source="rollover",
            plan_date=TODAY,
        )
        assert item.carried_from_id == newer.id
        db.refresh(older)
        db.refresh(newer)
        assert older.status == "carried"
        assert newer.status == "carried"


def test_midnight_plan_date_binding():
    with memory_db() as db, patch.object(plan_svc, "today_local", return_value=date(2026, 7, 25)):
        user = _user(db)
        todo = _todo(db, user, urgent=True, due=date(2026, 7, 20))
        try:
            plan_svc.confirm_urgent_decisions(
                db,
                user_id=user.id,
                plan_date=date(2026, 7, 24),
                decisions=[(todo.id, "keep")],
            )
            raise AssertionError("stale across midnight")
        except plan_svc.ReviewStaleError:
            pass
        kept, _ = plan_svc.confirm_urgent_decisions(
            db,
            user_id=user.id,
            plan_date=date(2026, 7, 25),
            decisions=[(todo.id, "keep")],
        )
        assert todo.id in kept


def test_double_confirm_idempotent_keep():
    with memory_db() as db, patch.object(plan_svc, "today_local", return_value=TODAY):
        user = _user(db)
        todo = _todo(db, user, urgent=True, due=TODAY - timedelta(days=1))
        plan_svc.confirm_urgent_decisions(
            db,
            user_id=user.id,
            plan_date=TODAY,
            decisions=[(todo.id, "keep")],
        )
        plan_svc.confirm_urgent_decisions(
            db,
            user_id=user.id,
            plan_date=TODAY,
            decisions=[(todo.id, "keep")],
        )
        rows = (
            db.query(TodoDailyPlanItem)
            .filter(
                TodoDailyPlanItem.todo_id == todo.id,
                TodoDailyPlanItem.plan_date == TODAY,
            )
            .all()
        )
        assert len(rows) == 1
        assert rows[0].status == "active"


def test_route_stale_returns_409_payload():
    from app.auth import get_current_user
    from app.database import get_db

    with memory_db() as db:
        user = _user(db)
        todo = _todo(db, user, urgent=True, due=TODAY - timedelta(days=1))
        db.commit()

        app = FastAPI()
        app.include_router(todos_routes.router, prefix="/api/todos")

        def _override_db():
            try:
                yield db
            finally:
                pass

        def _override_user():
            return user

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[get_current_user] = _override_user

        with patch.object(plan_svc, "today_local", return_value=TODAY), patch(
            "app.routes.todos.today_local", return_value=TODAY
        ), TestClient(app) as client:
            resp = client.post(
                "/api/todos/today-plan/urgent-confirm",
                json={
                    "plan_date": "2026-07-23",
                    "decisions": [{"todo_id": todo.id, "action": "keep"}],
                },
            )
            assert resp.status_code == 409
            body = resp.json()
            detail = body.get("detail") or body
            assert detail.get("code") == "REVIEW_STALE"


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
    print(f"r7_today_plan={len(tests)} failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
