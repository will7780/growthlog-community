"""Todo hierarchy primitives for GrowthLog.

The database stores only the immutable parent pointer. Depth, roots, paths and
subtree counts are derived from the current user-owned tree so they cannot drift.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Set

from sqlalchemy.orm import Session

from app.models import Todo

MAX_SUBTASK_DEPTH = 3
CODE_MAX_DEPTH = "TODO_MAX_DEPTH_REACHED"
CODE_DESCENDANTS_INCOMPLETE = "TODO_DESCENDANTS_INCOMPLETE"
CODE_TREE_STALE = "TODO_TREE_STALE"
CODE_ORDER_STALE = "TODO_ORDER_STALE"
CODE_HIERARCHY_CORRUPT = "TODO_HIERARCHY_CORRUPT"


class TodoHierarchyError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class TodoHierarchyIndex:
    by_id: Dict[int, Todo]
    children: Dict[Optional[int], List[int]]


def build_index(rows: Iterable[Todo]) -> TodoHierarchyIndex:
    by_id = {int(row.id): row for row in rows}
    children: Dict[Optional[int], List[int]] = {None: []}
    for row in rows:
        parent_id = int(row.parent_id) if getattr(row, "parent_id", None) is not None else None
        children.setdefault(parent_id, []).append(int(row.id))
        children.setdefault(int(row.id), [])
    for parent_id, ids in children.items():
        if parent_id is None:
            ids.sort()
            continue
        ids.sort(
            key=lambda item: (
                int(value)
                if (value := getattr(by_id[item], "sort_order", None)) is not None
                else 2**31,
                item,
            )
        )
    return TodoHierarchyIndex(by_id=by_id, children=children)


def load_index(db: Session, user_id: int, *, for_update: bool = False) -> TodoHierarchyIndex:
    query = db.query(Todo).filter(Todo.user_id == user_id).order_by(Todo.id.asc())
    if for_update:
        query = query.with_for_update()
    return build_index(query.all())


def ancestor_ids(todo_id: int, index: TodoHierarchyIndex) -> List[int]:
    current = index.by_id.get(int(todo_id))
    if current is None:
        return []
    ancestors: List[int] = []
    seen: Set[int] = {int(todo_id)}
    while getattr(current, "parent_id", None) is not None:
        parent_id = int(current.parent_id)
        if parent_id in seen:
            raise TodoHierarchyError(CODE_HIERARCHY_CORRUPT, "任务层级存在循环")
        parent = index.by_id.get(parent_id)
        if parent is None:
            raise TodoHierarchyError(CODE_HIERARCHY_CORRUPT, "任务父链不完整")
        ancestors.append(parent_id)
        seen.add(parent_id)
        current = parent
    ancestors.reverse()
    return ancestors


def depth_of(todo_id: int, index: TodoHierarchyIndex) -> int:
    return len(ancestor_ids(todo_id, index))


def family_root_id(todo_id: int, index: TodoHierarchyIndex) -> int:
    ancestors = ancestor_ids(todo_id, index)
    return ancestors[0] if ancestors else int(todo_id)


def descendant_ids(todo_id: int, index: TodoHierarchyIndex) -> List[int]:
    root = int(todo_id)
    if root not in index.by_id:
        return []
    out: List[int] = []
    stack = list(reversed(index.children.get(root, [])))
    seen: Set[int] = {root}
    while stack:
        current = stack.pop()
        if current in seen:
            raise TodoHierarchyError(CODE_HIERARCHY_CORRUPT, "任务层级存在循环")
        seen.add(current)
        out.append(current)
        stack.extend(reversed(index.children.get(current, [])))
    return out


def subtree_ids(todo_id: int, index: TodoHierarchyIndex) -> List[int]:
    return [int(todo_id), *descendant_ids(todo_id, index)]


def child_sort_order(todo: Todo) -> int:
    value = getattr(todo, "sort_order", None)
    return int(value) if value is not None else 2**31


def next_child_sort_order(parent_id: int, index: TodoHierarchyIndex) -> int:
    values = [
        child_sort_order(index.by_id[item])
        for item in index.children.get(int(parent_id), [])
        if child_sort_order(index.by_id[item]) < 2**31
    ]
    return max(values) + 1 if values else 0


def incomplete_descendant_ids(todo_id: int, index: TodoHierarchyIndex) -> List[int]:
    return [
        child_id
        for child_id in descendant_ids(todo_id, index)
        if not bool(index.by_id[child_id].is_done)
    ]


def metadata_for(todo_id: int, index: TodoHierarchyIndex) -> Dict[str, object]:
    tid = int(todo_id)
    ancestors = ancestor_ids(tid, index)
    descendants = descendant_ids(tid, index)
    return {
        "parent_id": (
            int(index.by_id[tid].parent_id)
            if getattr(index.by_id[tid], "parent_id", None) is not None
            else None
        ),
        "sort_order": (
            int(index.by_id[tid].sort_order)
            if getattr(index.by_id[tid], "sort_order", None) is not None
            else None
        ),
        "depth": len(ancestors),
        "family_root_id": ancestors[0] if ancestors else tid,
        "ancestor_titles": [str(index.by_id[item].content) for item in ancestors],
        "direct_child_count": len(index.children.get(tid, [])),
        "descendant_count": len(descendants),
        "incomplete_descendant_count": sum(
            1 for item in descendants if not bool(index.by_id[item].is_done)
        ),
    }


def metadata_map(index: TodoHierarchyIndex) -> Dict[int, Dict[str, object]]:
    return {todo_id: metadata_for(todo_id, index) for todo_id in index.by_id}


def assert_can_add_child(parent_id: int, index: TodoHierarchyIndex) -> int:
    parent = index.by_id.get(int(parent_id))
    if parent is None:
        raise LookupError("todo_not_found")
    parent_depth = depth_of(int(parent_id), index)
    if parent_depth >= MAX_SUBTASK_DEPTH:
        raise TodoHierarchyError(CODE_MAX_DEPTH, "最多支持三级子任务")
    return parent_depth + 1


def reopen_completed_ancestors(
    db: Session,
    *,
    user_id: int,
    todo_id: int,
    index: TodoHierarchyIndex,
    include_self: bool = False,
) -> List[int]:
    from app.services import todo_daily_plan as plan_svc

    ids = ancestor_ids(todo_id, index)
    if include_self:
        ids.append(int(todo_id))
    reopened: List[int] = []
    for item_id in ids:
        todo = index.by_id[item_id]
        if not bool(todo.is_done):
            continue
        todo.is_done = False
        todo.completed_at = None
        todo.completion_note = None
        plan_svc.sync_plan_on_todo_done_change(
            db,
            user_id=user_id,
            todo_id=item_id,
            is_done=False,
        )
        reopened.append(item_id)
    return reopened


def collapse_topmost_candidate_ids(
    candidate_ids: Iterable[int],
    index: TodoHierarchyIndex,
) -> List[int]:
    candidates = {int(item) for item in candidate_ids if int(item) in index.by_id}
    out: List[int] = []
    for item_id in sorted(candidates):
        if any(ancestor in candidates for ancestor in ancestor_ids(item_id, index)):
            continue
        out.append(item_id)
    return out


def assert_expected_descendant_count(
    todo_id: int,
    expected_count: Optional[int],
    index: TodoHierarchyIndex,
    *,
    required_when_nonzero: bool = True,
) -> int:
    actual = len(descendant_ids(todo_id, index))
    if expected_count is None:
        if required_when_nonzero and actual:
            raise TodoHierarchyError(CODE_TREE_STALE, "任务树已变化，请刷新后重试")
        return actual
    if int(expected_count) != actual:
        raise TodoHierarchyError(CODE_TREE_STALE, "任务树已变化，请刷新后重试")
    return actual