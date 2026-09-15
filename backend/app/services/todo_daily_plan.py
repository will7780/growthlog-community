"""
Today's Plan arrangement layer for existing Todos.

R7.1: preview/confirm binding, explicit reviewed sets, lineage-safe carry,
row locks, and all-or-nothing validation.
"""
from __future__ import annotations

from datetime import date
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Todo, TodoDailyPlanItem
from app.timeutil import today_local
from app.services import todo_hierarchy as hierarchy

PLAN_ACTIVE = "active"
PLAN_COMPLETED = "completed"
PLAN_CARRIED = "carried"
PLAN_RETURNED = "returned"
SOURCE_MANUAL = "manual"
SOURCE_ROLLOVER = "rollover"

REASON_OVERDUE = "overdue"
REASON_PAST_ACTIVE_PLAN = "past_active_plan"

CODE_REVIEW_STALE = "REVIEW_STALE"
CODE_REVIEW_REJECTED = "REVIEW_REJECTED"


class ReviewStaleError(Exception):
    """plan_date from preview does not match backend today_local()."""

    def __init__(self, message: str = "日期或任务状态已变化，请刷新后重试"):
        super().__init__(message)
        self.code = CODE_REVIEW_STALE


class ReviewRejectedError(Exception):
    """Confirm payload invalid or targets non-candidates."""

    def __init__(self, message: str):
        super().__init__(message)
        self.code = CODE_REVIEW_REJECTED


def assert_plan_date_fresh(plan_date: date) -> date:
    today = today_local()
    if plan_date != today:
        raise ReviewStaleError()
    return today


def _owned_todo(db: Session, user_id: int, todo_id: int, *, for_update: bool = False) -> Optional[Todo]:
    q = db.query(Todo).filter(Todo.id == todo_id, Todo.user_id == user_id)
    if for_update:
        q = q.with_for_update()
    return q.first()


def _lock_todos_ordered(db: Session, user_id: int, todo_ids: Iterable[int]) -> Dict[int, Todo]:
    ids = sorted({int(x) for x in todo_ids})
    if not ids:
        return {}
    rows = (
        db.query(Todo)
        .filter(Todo.user_id == user_id, Todo.id.in_(ids))
        .order_by(Todo.id.asc())
        .with_for_update()
        .all()
    )
    return {int(r.id): r for r in rows}


def _lock_plans_for_todos(
    db: Session,
    *,
    user_id: int,
    todo_ids: Iterable[int],
) -> List[TodoDailyPlanItem]:
    ids = sorted({int(x) for x in todo_ids})
    if not ids:
        return []
    return (
        db.query(TodoDailyPlanItem)
        .filter(
            TodoDailyPlanItem.user_id == user_id,
            TodoDailyPlanItem.todo_id.in_(ids),
        )
        .order_by(TodoDailyPlanItem.todo_id.asc(), TodoDailyPlanItem.plan_date.asc(), TodoDailyPlanItem.id.asc())
        .with_for_update()
        .all()
    )


def find_latest_past_active(
    db: Session,
    *,
    user_id: int,
    todo_id: int,
    before_date: date,
) -> Optional[TodoDailyPlanItem]:
    """Read-only: newest past active plan row (does not mutate status)."""
    return (
        db.query(TodoDailyPlanItem)
        .filter(
            TodoDailyPlanItem.user_id == user_id,
            TodoDailyPlanItem.todo_id == todo_id,
            TodoDailyPlanItem.status == PLAN_ACTIVE,
            TodoDailyPlanItem.plan_date < before_date,
        )
        .order_by(TodoDailyPlanItem.plan_date.desc(), TodoDailyPlanItem.id.desc())
        .first()
    )


def mark_past_actives_carried(
    db: Session,
    *,
    user_id: int,
    todo_id: int,
    before_date: date,
) -> None:
    (
        db.query(TodoDailyPlanItem)
        .filter(
            TodoDailyPlanItem.user_id == user_id,
            TodoDailyPlanItem.todo_id == todo_id,
            TodoDailyPlanItem.status == PLAN_ACTIVE,
            TodoDailyPlanItem.plan_date < before_date,
        )
        .update({TodoDailyPlanItem.status: PLAN_CARRIED}, synchronize_session=False)
    )


def add_todo_to_today(
    db: Session,
    *,
    user_id: int,
    todo_id: int,
    source: str = SOURCE_MANUAL,
    plan_date: Optional[date] = None,
    lock: bool = True,
) -> TodoDailyPlanItem:
    """
    Idempotent upsert of today's plan item.
    Lineage: capture latest past active id BEFORE marking carried.
    """
    today = plan_date or today_local()
    if lock:
        todo = _owned_todo(db, user_id, todo_id, for_update=True)
        _lock_plans_for_todos(db, user_id=user_id, todo_ids=[todo_id])
    else:
        todo = _owned_todo(db, user_id, todo_id)
    if todo is None:
        raise LookupError("todo_not_found")

    latest_past = find_latest_past_active(
        db, user_id=user_id, todo_id=todo_id, before_date=today
    )
    carried_from_id = int(latest_past.id) if latest_past is not None else None

    existing = (
        db.query(TodoDailyPlanItem)
        .filter(
            TodoDailyPlanItem.user_id == user_id,
            TodoDailyPlanItem.todo_id == todo_id,
            TodoDailyPlanItem.plan_date == today,
        )
        .first()
    )
    if existing is not None:
        if existing.status == PLAN_RETURNED:
            existing.status = PLAN_ACTIVE
            existing.source = source
            if carried_from_id is not None:
                existing.carried_from_id = carried_from_id
        elif existing.status == PLAN_COMPLETED and not todo.is_done:
            existing.status = PLAN_ACTIVE
            if carried_from_id is not None and existing.carried_from_id is None:
                existing.carried_from_id = carried_from_id
        elif existing.status == PLAN_CARRIED:
            existing.status = PLAN_ACTIVE
            existing.source = source
            if carried_from_id is not None:
                existing.carried_from_id = carried_from_id
        elif existing.status == PLAN_ACTIVE and carried_from_id is not None and existing.carried_from_id is None:
            existing.carried_from_id = carried_from_id
        if todo.is_done and existing.status == PLAN_ACTIVE:
            existing.status = PLAN_COMPLETED
        mark_past_actives_carried(db, user_id=user_id, todo_id=todo_id, before_date=today)
        db.flush()
        return existing

    try:
        with db.begin_nested():
            item = TodoDailyPlanItem(
                user_id=user_id,
                todo_id=todo_id,
                plan_date=today,
                source=source,
                status=PLAN_COMPLETED if todo.is_done else PLAN_ACTIVE,
                carried_from_id=carried_from_id,
            )
            db.add(item)
            db.flush()
            mark_past_actives_carried(db, user_id=user_id, todo_id=todo_id, before_date=today)
            db.flush()
            return item
    except IntegrityError:
        db.expire_all()
        raced = (
            db.query(TodoDailyPlanItem)
            .filter(
                TodoDailyPlanItem.user_id == user_id,
                TodoDailyPlanItem.todo_id == todo_id,
                TodoDailyPlanItem.plan_date == today,
            )
            .with_for_update()
            .first()
        )
        if raced is None:
            raise
        if raced.status == PLAN_RETURNED:
            raced.status = PLAN_ACTIVE
            raced.source = source
            if carried_from_id is not None:
                raced.carried_from_id = carried_from_id
        elif raced.status == PLAN_COMPLETED and not todo.is_done:
            raced.status = PLAN_ACTIVE
        if todo.is_done and raced.status == PLAN_ACTIVE:
            raced.status = PLAN_COMPLETED
        if carried_from_id is not None and raced.carried_from_id is None:
            raced.carried_from_id = carried_from_id
        mark_past_actives_carried(db, user_id=user_id, todo_id=todo_id, before_date=today)
        db.flush()
        return raced


def remove_todo_from_today(
    db: Session,
    *,
    user_id: int,
    todo_id: int,
    plan_date: Optional[date] = None,
) -> Optional[TodoDailyPlanItem]:
    today = plan_date or today_local()
    todo = _owned_todo(db, user_id, todo_id, for_update=True)
    if todo is None:
        raise LookupError("todo_not_found")
    _lock_plans_for_todos(db, user_id=user_id, todo_ids=[todo_id])
    item = (
        db.query(TodoDailyPlanItem)
        .filter(
            TodoDailyPlanItem.user_id == user_id,
            TodoDailyPlanItem.todo_id == todo_id,
            TodoDailyPlanItem.plan_date == today,
        )
        .first()
    )
    if item is None:
        return None
    if item.status in {PLAN_ACTIVE, PLAN_COMPLETED}:
        item.status = PLAN_RETURNED
    db.flush()
    return item


def add_todo_subtree_to_today(
    db: Session,
    *,
    user_id: int,
    todo_id: int,
    source: str = SOURCE_MANUAL,
    plan_date: Optional[date] = None,
) -> TodoDailyPlanItem:
    """Add the selected node and every unfinished descendant, idempotently."""
    today = plan_date or today_local()
    index = hierarchy.load_index(db, user_id, for_update=True)
    if int(todo_id) not in index.by_id:
        raise LookupError("todo_not_found")
    subtree = hierarchy.subtree_ids(int(todo_id), index)
    target_ids = [
        item_id
        for item_id in subtree
        if item_id == int(todo_id) or not bool(index.by_id[item_id].is_done)
    ]
    _lock_plans_for_todos(db, user_id=user_id, todo_ids=target_ids)
    root_item: Optional[TodoDailyPlanItem] = None
    for item_id in sorted(target_ids):
        item = add_todo_to_today(
            db,
            user_id=user_id,
            todo_id=item_id,
            source=source,
            plan_date=today,
            lock=False,
        )
        if item_id == int(todo_id):
            root_item = item
    if root_item is None:
        raise LookupError("todo_not_found")
    return root_item


def remove_todo_subtree_from_today(
    db: Session,
    *,
    user_id: int,
    todo_id: int,
    plan_date: Optional[date] = None,
) -> List[int]:
    """Return every current-day plan row in the selected subtree."""
    today = plan_date or today_local()
    index = hierarchy.load_index(db, user_id, for_update=True)
    if int(todo_id) not in index.by_id:
        raise LookupError("todo_not_found")
    subtree = hierarchy.subtree_ids(int(todo_id), index)
    _lock_plans_for_todos(db, user_id=user_id, todo_ids=subtree)
    rows = (
        db.query(TodoDailyPlanItem)
        .filter(
            TodoDailyPlanItem.user_id == user_id,
            TodoDailyPlanItem.todo_id.in_(subtree),
            TodoDailyPlanItem.plan_date == today,
            TodoDailyPlanItem.status.in_([PLAN_ACTIVE, PLAN_COMPLETED]),
        )
        .order_by(TodoDailyPlanItem.todo_id.asc())
        .all()
    )
    changed: List[int] = []
    for row in rows:
        row.status = PLAN_RETURNED
        changed.append(int(row.todo_id))
    db.flush()
    return changed

def sync_plan_on_todo_done_change(
    db: Session,
    *,
    user_id: int,
    todo_id: int,
    is_done: bool,
    plan_date: Optional[date] = None,
) -> None:
    today = plan_date or today_local()
    if is_done:
        (
            db.query(TodoDailyPlanItem)
            .filter(
                TodoDailyPlanItem.user_id == user_id,
                TodoDailyPlanItem.todo_id == todo_id,
                TodoDailyPlanItem.status == PLAN_ACTIVE,
            )
            .update({TodoDailyPlanItem.status: PLAN_COMPLETED}, synchronize_session=False)
        )
    else:
        (
            db.query(TodoDailyPlanItem)
            .filter(
                TodoDailyPlanItem.user_id == user_id,
                TodoDailyPlanItem.todo_id == todo_id,
                TodoDailyPlanItem.plan_date == today,
                TodoDailyPlanItem.status == PLAN_COMPLETED,
            )
            .update({TodoDailyPlanItem.status: PLAN_ACTIVE}, synchronize_session=False)
        )
    db.flush()


def list_today_plan_items(
    db: Session,
    *,
    user_id: int,
    plan_date: Optional[date] = None,
) -> List[TodoDailyPlanItem]:
    today = plan_date or today_local()
    return (
        db.query(TodoDailyPlanItem)
        .filter(
            TodoDailyPlanItem.user_id == user_id,
            TodoDailyPlanItem.plan_date == today,
            TodoDailyPlanItem.status.in_([PLAN_ACTIVE, PLAN_COMPLETED]),
        )
        .all()
    )


def today_plan_todo_ids(
    db: Session,
    *,
    user_id: int,
    plan_date: Optional[date] = None,
) -> Set[int]:
    return {int(item.todo_id) for item in list_today_plan_items(db, user_id=user_id, plan_date=plan_date)}


def _latest_past_active_by_todo(
    db: Session,
    *,
    user_id: int,
    before_date: date,
) -> Dict[int, TodoDailyPlanItem]:
    rows = (
        db.query(TodoDailyPlanItem)
        .filter(
            TodoDailyPlanItem.user_id == user_id,
            TodoDailyPlanItem.status == PLAN_ACTIVE,
            TodoDailyPlanItem.plan_date < before_date,
        )
        .order_by(TodoDailyPlanItem.plan_date.desc(), TodoDailyPlanItem.id.desc())
        .all()
    )
    latest: Dict[int, TodoDailyPlanItem] = {}
    for row in rows:
        tid = int(row.todo_id)
        if tid not in latest:
            latest[tid] = row
    return latest


def list_urgent_candidates(
    db: Session,
    *,
    user_id: int,
    plan_date: Optional[date] = None,
) -> List[Tuple[Todo, List[str], Optional[date]]]:
    today = plan_date or today_local()
    today_active_ids = {
        int(r.todo_id)
        for r in db.query(TodoDailyPlanItem.todo_id)
        .filter(
            TodoDailyPlanItem.user_id == user_id,
            TodoDailyPlanItem.plan_date == today,
            TodoDailyPlanItem.status == PLAN_ACTIVE,
        )
        .all()
    }
    past_map = _latest_past_active_by_todo(db, user_id=user_id, before_date=today)
    todos = (
        db.query(Todo)
        .filter(
            Todo.user_id == user_id,
            Todo.parent_id.is_(None),
            Todo.is_done == False,  # noqa: E712
            Todo.is_urgent == True,  # noqa: E712
        )
        .all()
    )
    out: List[Tuple[Todo, List[str], Optional[date]]] = []
    for todo in todos:
        if int(todo.id) in today_active_ids:
            continue
        reasons: List[str] = []
        past_date: Optional[date] = None
        if todo.due_date is not None and todo.due_date < today:
            reasons.append(REASON_OVERDUE)
        past = past_map.get(int(todo.id))
        if past is not None:
            reasons.append(REASON_PAST_ACTIVE_PLAN)
            past_date = past.plan_date
        if reasons:
            out.append((todo, reasons, past_date))
    index = hierarchy.load_index(db, user_id)
    by_id = {int(todo.id): (todo, reasons, past_date) for todo, reasons, past_date in out}
    top_ids = hierarchy.collapse_topmost_candidate_ids(by_id.keys(), index)
    return [by_id[item_id] for item_id in top_ids]

def list_rollover_candidates(
    db: Session,
    *,
    user_id: int,
    plan_date: Optional[date] = None,
    exclude_todo_ids: Optional[Iterable[int]] = None,
) -> List[Tuple[Todo, TodoDailyPlanItem]]:
    today = plan_date or today_local()
    excluded = {int(x) for x in (exclude_todo_ids or [])}
    past_map = _latest_past_active_by_todo(db, user_id=user_id, before_date=today)
    if not past_map:
        return []
    todos = (
        db.query(Todo)
        .filter(
            Todo.user_id == user_id,
            Todo.id.in_(list(past_map.keys())),
            Todo.parent_id.is_(None),
            Todo.is_done == False,  # noqa: E712
            Todo.is_urgent == False,  # noqa: E712
        )
        .all()
    )
    todo_by_id = {int(t.id): t for t in todos}
    out: List[Tuple[Todo, TodoDailyPlanItem]] = []
    for tid, plan_item in past_map.items():
        if tid in excluded:
            continue
        todo = todo_by_id.get(tid)
        if todo is None:
            continue
        out.append((todo, plan_item))
    index = hierarchy.load_index(db, user_id)
    by_id = {int(todo.id): (todo, plan_item) for todo, plan_item in out}
    top_ids = hierarchy.collapse_topmost_candidate_ids(by_id.keys(), index)
    return [by_id[item_id] for item_id in top_ids]

def _has_today_active(db: Session, user_id: int, todo_id: int, today: date) -> bool:
    row = (
        db.query(TodoDailyPlanItem.id)
        .filter(
            TodoDailyPlanItem.user_id == user_id,
            TodoDailyPlanItem.todo_id == todo_id,
            TodoDailyPlanItem.plan_date == today,
            TodoDailyPlanItem.status == PLAN_ACTIVE,
        )
        .first()
    )
    return row is not None


def confirm_urgent_decisions(
    db: Session,
    *,
    user_id: int,
    plan_date: date,
    decisions: Sequence[Tuple],
) -> Tuple[List[int], List[int]]:
    today = assert_plan_date_fresh(plan_date)

    seen: Dict[int, Tuple[str, Optional[int]]] = {}
    for decision in decisions:
        if len(decision) == 2:
            todo_id, action = decision
            expected_count = None
        else:
            todo_id, action, expected_count = decision
        tid = int(todo_id)
        act = str(action)
        if act not in {"keep", "delete"}:
            raise ReviewRejectedError(f"无效操作: {act}")
        if tid in seen:
            if seen[tid][0] != act:
                raise ReviewRejectedError("同一任务存在冲突操作")
            raise ReviewRejectedError("重复的任务决策")
        seen[tid] = (act, int(expected_count) if expected_count is not None else None)

    index = hierarchy.load_index(db, user_id, for_update=True)
    todo_ids = sorted(seen)
    plan_ids: Set[int] = set()
    for tid in todo_ids:
        if tid in index.by_id:
            plan_ids.update(hierarchy.subtree_ids(tid, index))
    _lock_plans_for_todos(db, user_id=user_id, todo_ids=plan_ids)

    candidates = {
        int(todo.id)
        for todo, _reasons, _past in list_urgent_candidates(
            db, user_id=user_id, plan_date=today
        )
    }

    for tid, (action, expected_count) in seen.items():
        todo = index.by_id.get(tid)
        if todo is None:
            if action == "delete":
                exists = db.query(Todo.id).filter(Todo.id == tid).first()
                if exists is not None:
                    raise ReviewRejectedError("任务不存在或不属于当前用户")
                continue
            raise ReviewRejectedError("任务不存在或不属于当前用户")
        if expected_count is not None:
            try:
                hierarchy.assert_expected_descendant_count(
                    tid, expected_count, index, required_when_nonzero=False
                )
            except hierarchy.TodoHierarchyError as exc:
                raise ReviewStaleError(str(exc)) from exc
        if tid in candidates:
            continue
        if action == "keep" and _has_today_active(db, user_id, tid, today):
            continue
        raise ReviewRejectedError("只能处理当前紧急审核候选任务")

    kept: List[int] = []
    deleted: List[int] = []
    for tid, (action, _expected_count) in seen.items():
        todo = index.by_id.get(tid)
        if action == "keep":
            if todo is None:
                continue
            add_todo_subtree_to_today(
                db,
                user_id=user_id,
                todo_id=tid,
                source=SOURCE_ROLLOVER,
                plan_date=today,
            )
            kept.append(tid)
        else:
            if todo is not None:
                db.delete(todo)
            deleted.append(tid)
    db.flush()
    return kept, deleted

def confirm_rollover(
    db: Session,
    *,
    user_id: int,
    plan_date: date,
    reviewed_todo_ids: Sequence[int],
    selected_todo_ids: Sequence[int],
    expected_descendant_counts: Optional[Dict[int, int]] = None,
) -> Tuple[List[int], List[int]]:
    """Apply each reviewed candidate to its complete subtree."""
    today = assert_plan_date_fresh(plan_date)
    reviewed = sorted({int(item) for item in reviewed_todo_ids})
    selected = {int(item) for item in selected_todo_ids}
    if not selected.issubset(set(reviewed)):
        raise ReviewRejectedError("selected_todo_ids 必须是 reviewed_todo_ids 的子集")
    if not reviewed:
        return [], []

    index = hierarchy.load_index(db, user_id, for_update=True)
    plan_ids: Set[int] = set()
    for tid in reviewed:
        if tid in index.by_id:
            plan_ids.update(hierarchy.subtree_ids(tid, index))
    _lock_plans_for_todos(db, user_id=user_id, todo_ids=plan_ids)

    expected = {int(key): int(value) for key, value in (expected_descendant_counts or {}).items()}
    for tid, count in expected.items():
        if tid in reviewed and tid in index.by_id:
            try:
                hierarchy.assert_expected_descendant_count(
                    tid, count, index, required_when_nonzero=False
                )
            except hierarchy.TodoHierarchyError as exc:
                raise ReviewStaleError(str(exc)) from exc

    urgent_roots = {
        int(todo.id)
        for todo, _reasons, _past in list_urgent_candidates(
            db, user_id=user_id, plan_date=today
        )
    }
    urgent_tree_ids: Set[int] = set()
    for root_id in urgent_roots:
        if root_id in index.by_id:
            urgent_tree_ids.update(hierarchy.subtree_ids(root_id, index))

    live_map = {
        int(todo.id): (todo, past)
        for todo, past in list_rollover_candidates(
            db,
            user_id=user_id,
            plan_date=today,
            exclude_todo_ids=urgent_tree_ids,
        )
    }

    added: List[int] = []
    returned: List[int] = []
    for tid in reviewed:
        if tid in urgent_tree_ids:
            continue
        todo = index.by_id.get(tid)
        if todo is None:
            continue
        subtree = hierarchy.subtree_ids(tid, index)
        if tid in selected:
            if tid not in live_map and not _has_today_active(db, user_id, tid, today):
                continue
            add_todo_subtree_to_today(
                db,
                user_id=user_id,
                todo_id=tid,
                source=SOURCE_ROLLOVER,
                plan_date=today,
            )
            added.append(tid)
            continue

        changed = (
            db.query(TodoDailyPlanItem)
            .filter(
                TodoDailyPlanItem.user_id == user_id,
                TodoDailyPlanItem.todo_id.in_(subtree),
                TodoDailyPlanItem.status == PLAN_ACTIVE,
                TodoDailyPlanItem.plan_date < today,
            )
            .update({TodoDailyPlanItem.status: PLAN_RETURNED}, synchronize_session=False)
        )
        if changed or tid in live_map:
            returned.append(tid)
    db.flush()
    return added, returned