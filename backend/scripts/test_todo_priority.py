from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
import sys

from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.routes.todos import build_todo_response, normalize_priority, todo_sort_key
from app.schemas.todos import TodoCreateRequest, TodoUpdateRequest


def make_todo(**overrides):
    values = {
        "id": 1,
        "content": "test",
        "priority": "P4",
        "due_date": None,
        "is_done": False,
        "is_urgent": False,
        "completed_at": None,
        "created_at": datetime(2026, 7, 17, 8, 0, 0),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_schema_defaults_and_rejects_invalid_priority():
    assert TodoCreateRequest(content="a").priority == "P4"
    assert TodoCreateRequest(content="a", priority="P0").priority == "P0"
    assert TodoUpdateRequest(priority="P2").priority == "P2"
    try:
        TodoCreateRequest(content="a", priority="P5")
    except ValidationError:
        pass
    else:
        raise AssertionError("P5 must be rejected")


def test_sort_priority_due_date_and_done_order():
    items = [
        make_todo(id=1, priority="P2", due_date=date(2026, 7, 18)),
        make_todo(id=2, priority="P0", due_date=None),
        make_todo(id=3, priority="P0", due_date=date(2026, 7, 19)),
        make_todo(id=4, priority="P1", is_done=True, completed_at=datetime(2026, 7, 17, 9, 0, 0)),
        make_todo(id=5, priority="P4", is_urgent=True, due_date=None),
    ]
    assert [item.id for item in sorted(items, key=todo_sort_key)] == [5, 3, 2, 1, 4]


def test_response_normalizes_historical_values():
    assert normalize_priority(None) == "P4"
    assert normalize_priority("bad") == "P4"
    response = build_todo_response(make_todo(priority=None))
    assert response.priority == "P4"


def test_migration_is_idempotent_and_scoped():
    text = (ROOT / "backend" / "migrations" / "020_add_priority_to_todos.sql").read_text(encoding="utf-8")
    assert "INFORMATION_SCHEMA.COLUMNS" in text
    assert "INFORMATION_SCHEMA.STATISTICS" in text
    assert "DEFAULT ''P4''" in text
    assert "NOT IN ('P0', 'P1', 'P2', 'P3', 'P4')" in text
    assert "idx_todos_user_priority" in text


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"todo_priority_tests={len(tests)} failed=0")
