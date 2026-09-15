"""
Todo completion_note focused tests (no migration execute, no DB writes).

Run from backend/:
  python scripts/test_todo_completion_note.py
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
sys.path.insert(0, str(BACKEND_ROOT))


def test_migration_024_idempotent() -> None:
    sql = (BACKEND_ROOT / "migrations" / "024_add_todos_completion_note.sql").read_text(
        encoding="utf-8"
    )
    assert "completion_note" in sql
    assert "VARCHAR(1000)" in sql
    assert "INFORMATION_SCHEMA.COLUMNS" in sql
    assert "PREPARE" in sql
    print("PASS migration_024_idempotent")


def test_schema_and_response_include_note() -> None:
    from app.models.todo import Todo
    from app.schemas.todos import TodoResponse, TodoUpdateRequest

    assert "completion_note" in Todo.__table__.c
    assert Todo.__table__.c.completion_note.nullable is True

    body = TodoUpdateRequest(is_done=True, completion_note="  学到了索引  ")
    assert body.completion_note == "  学到了索引  "

    resp = TodoResponse(
        id=1,
        content="写测试",
        priority="P4",
        due_date=None,
        is_done=True,
        is_urgent=False,
        is_expiring_soon=False,
        is_overdue=False,
        completed_at=datetime(2026, 7, 27),
        completion_note="有收获",
        created_at=datetime(2026, 7, 27),
    )
    assert resp.completion_note == "有收获"
    print("PASS schema_and_response_include_note")


def test_update_todo_sets_and_clears_note() -> None:
    from fastapi import HTTPException

    from app.routes import todos as todos_mod
    from app.schemas.todos import TodoUpdateRequest
    from app.services.todo_hierarchy import TodoHierarchyIndex

    todo = SimpleNamespace(
        id=3,
        user_id=1,
        parent_id=None,
        content="复盘",
        priority="P3",
        due_date=None,
        is_done=False,
        is_urgent=False,
        completed_at=None,
        completion_note=None,
        created_at=datetime(2026, 7, 27),
    )
    index = TodoHierarchyIndex(by_id={3: todo}, children={None: [3], 3: []})
    db = MagicMock()
    user = SimpleNamespace(id=1)

    with patch.object(todos_mod.hierarchy, "load_index", return_value=index):
        with patch.object(todos_mod.plan_svc, "sync_plan_on_todo_done_change"):
            with patch.object(todos_mod, "now_local", return_value=datetime(2026, 7, 27, 12, 0, 0)):
                resp = todos_mod.update_todo(
                    3,
                    TodoUpdateRequest(is_done=True, completion_note="  今天搞懂了锁  "),
                    current_user=user,  # type: ignore[arg-type]
                    db=db,
                )
    assert todo.is_done is True
    assert todo.completion_note == "今天搞懂了锁"
    assert resp.completion_note == "今天搞懂了锁"

    with patch.object(todos_mod.hierarchy, "load_index", return_value=index):
        with patch.object(todos_mod.plan_svc, "sync_plan_on_todo_done_change"):
            todos_mod.update_todo(
                3,
                TodoUpdateRequest(is_done=False),
                current_user=user,  # type: ignore[arg-type]
                db=db,
            )
    assert todo.is_done is False
    assert todo.completion_note is None

    todo.is_done = False
    with patch.object(todos_mod.hierarchy, "load_index", return_value=index):
        try:
            todos_mod.update_todo(
                3,
                TodoUpdateRequest(completion_note="不应写入"),
                current_user=user,  # type: ignore[arg-type]
                db=db,
            )
            raise AssertionError("expected 400")
        except HTTPException as exc:
            assert exc.status_code == 400
    print("PASS update_todo_sets_and_clears_note")

def test_frontend_complete_dialog_contract() -> None:
    item = (
        REPO_ROOT / "frontend" / "src" / "components" / "todos" / "TodoItem.tsx"
    ).read_text(encoding="utf-8")
    assert "有什么收获和心得" in item
    assert "completion_note" in item
    assert "todo-complete-dialog" in item
    assert "window.confirm" not in item
    ctx = (REPO_ROOT / "frontend" / "src" / "contexts" / "TodoContext.tsx").read_text(
        encoding="utf-8"
    )
    assert "completion_note" in ctx
    print("PASS frontend_complete_dialog_contract")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
    print(f"todo_completion_note={len(tests)} failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
