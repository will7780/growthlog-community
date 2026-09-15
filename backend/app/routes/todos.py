"""Todo routes: flat compatibility, hierarchy tree and Today's Plan."""
from datetime import date as date_cls
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.auth import User, get_current_user
from app.database import get_db
from app.models import Todo
from app.schemas import (
    TodoChildCreateRequest,
    TodoChildrenOrderRequest,
    TodoChildrenOrderResponse,
    TodoCreateRequest,
    TodoListResponse,
    TodoResponse,
    TodoTreeListResponse,
    TodoTreeNode,
    TodoUpdateRequest,
    WeeklyStatsResponse,
)
from app.schemas.todos import (
    RolloverConfirmRequest,
    RolloverConfirmResponse,
    RolloverPreviewItem,
    RolloverPreviewResponse,
    TodayPlanItemResponse,
    TodayPlanResponse,
    UrgentConfirmRequest,
    UrgentConfirmResponse,
    UrgentPreviewItem,
    UrgentPreviewResponse,
)
from app.services import todo_daily_plan as plan_svc
from app.services import todo_hierarchy as hierarchy
from app.services.todo_knowledge import best_effort_sync_todo_ids, delete_todo_knowledge
from app.timeutil import now_local, today_local

router = APIRouter()
EXPIRING_DAYS = 3
PRIORITY_RANKS = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "P4": 4}
DATE_MAX = date_cls.max


def is_expiring_soon(due_date) -> bool:
    if due_date is None:
        return False
    delta = (due_date - today_local()).days
    return 0 <= delta <= EXPIRING_DAYS


def is_overdue(todo: Todo) -> bool:
    return bool(not todo.is_done and todo.due_date is not None and todo.due_date < today_local())


def normalize_priority(value: Optional[str]) -> str:
    normalized = (value or "P4").upper()
    return normalized if normalized in PRIORITY_RANKS else "P4"


def todo_sort_key(todo: Todo):
    if todo.is_done:
        completed_ts = todo.completed_at.timestamp() if todo.completed_at else 0
        return (1, 0, 0, True, DATE_MAX, -completed_ts)
    created_ts = todo.created_at.timestamp() if todo.created_at else 0
    return (
        0,
        0 if bool(getattr(todo, "is_urgent", False)) else 1,
        PRIORITY_RANKS[normalize_priority(todo.priority)],
        todo.due_date is None,
        todo.due_date or DATE_MAX,
        -created_ts,
    )


def _normalize_completion_note(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = value.strip()
    return text[:1000] if text else None


def _model_fields_set(model) -> set:
    return set(getattr(model, "model_fields_set", getattr(model, "__fields_set__", set())))


def _model_dump(model) -> Dict:
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


def build_todo_response(
    todo: Todo,
    metadata: Optional[Dict[str, object]] = None,
) -> TodoResponse:
    meta = metadata or {
        "parent_id": int(todo.parent_id) if getattr(todo, "parent_id", None) is not None else None,
        "sort_order": int(todo.sort_order) if getattr(todo, "sort_order", None) is not None else None,
        "depth": 0,
        "family_root_id": int(todo.id),
        "ancestor_titles": [],
        "direct_child_count": 0,
        "descendant_count": 0,
        "incomplete_descendant_count": 0,
    }
    return TodoResponse(
        id=int(todo.id),
        content=todo.content,
        priority=normalize_priority(todo.priority),
        due_date=todo.due_date,
        is_done=bool(todo.is_done),
        is_urgent=bool(getattr(todo, "is_urgent", False)),
        is_expiring_soon=is_expiring_soon(todo.due_date) if not todo.is_done else False,
        is_overdue=is_overdue(todo),
        completed_at=todo.completed_at,
        completion_note=getattr(todo, "completion_note", None),
        created_at=todo.created_at,
        **meta,
    )


def _todo_or_404(db: Session, user_id: int, todo_id: int) -> Todo:
    todo = db.query(Todo).filter(Todo.id == todo_id, Todo.user_id == user_id).first()
    if todo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="小要事不存在")
    return todo


def _hierarchy_http_error(exc: hierarchy.TodoHierarchyError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"code": exc.code, "message": str(exc)},
    )


def _tree_node(
    todo_id: int,
    index: hierarchy.TodoHierarchyIndex,
    meta: Dict[int, Dict[str, object]],
) -> TodoTreeNode:
    todo = index.by_id[todo_id]
    child_ids = list(index.children.get(todo_id, []))
    response = build_todo_response(todo, meta[todo_id])
    return TodoTreeNode(
        **_model_dump(response),
        children=[_tree_node(child_id, index, meta) for child_id in child_ids],
    )


@router.get("", response_model=TodoListResponse)
def get_todos(
    status_filter: Optional[str] = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    index = hierarchy.load_index(db, current_user.id)
    rows = list(index.by_id.values())
    if status_filter == "pending":
        rows = [todo for todo in rows if not todo.is_done]
    elif status_filter == "done":
        rows = [todo for todo in rows if todo.is_done]
    rows.sort(key=todo_sort_key)
    meta = hierarchy.metadata_map(index)
    return TodoListResponse(
        items=[build_todo_response(todo, meta[int(todo.id)]) for todo in rows[offset : offset + limit]],
        total=len(rows),
        limit=limit,
        offset=offset,
    )


@router.get("/tree", response_model=TodoTreeListResponse)
def get_todo_tree(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    index = hierarchy.load_index(db, current_user.id)
    meta = hierarchy.metadata_map(index)
    roots = [todo for todo in index.by_id.values() if getattr(todo, "parent_id", None) is None]
    roots.sort(key=todo_sort_key)
    page = roots[offset : offset + limit]
    return TodoTreeListResponse(
        items=[_tree_node(int(todo.id), index, meta) for todo in page],
        total=len(roots),
        limit=limit,
        offset=offset,
    )


@router.post("", response_model=TodoResponse, status_code=status.HTTP_201_CREATED)
def create_todo(
    request: TodoCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    todo = Todo(
        user_id=current_user.id,
        parent_id=None,
        sort_order=None,
        content=request.content,
        priority=request.priority,
        due_date=request.due_date,
        is_done=False,
        is_urgent=bool(request.is_urgent),
    )
    db.add(todo)
    db.flush()
    if request.add_to_today:
        plan_svc.add_todo_subtree_to_today(
            db,
            user_id=current_user.id,
            todo_id=int(todo.id),
            source=plan_svc.SOURCE_MANUAL,
        )
    db.commit()
    best_effort_sync_todo_ids(db, user_id=current_user.id, todo_ids=[int(todo.id)])
    index = hierarchy.load_index(db, current_user.id)
    return build_todo_response(index.by_id[int(todo.id)], hierarchy.metadata_for(int(todo.id), index))


@router.post("/{parent_id}/children", response_model=TodoResponse, status_code=status.HTTP_201_CREATED)
def create_child_todo(
    parent_id: int,
    request: TodoChildCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    index = hierarchy.load_index(db, current_user.id, for_update=True)
    parent = index.by_id.get(int(parent_id))
    if parent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="小要事不存在")
    try:
        hierarchy.assert_can_add_child(int(parent_id), index)
    except hierarchy.TodoHierarchyError as exc:
        raise _hierarchy_http_error(exc) from exc

    planned_ids = plan_svc.today_plan_todo_ids(db, user_id=current_user.id)
    root_id = hierarchy.family_root_id(int(parent.id), index)
    add_to_today = root_id in planned_ids
    child = Todo(
        user_id=current_user.id,
        parent_id=int(parent.id),
        sort_order=hierarchy.next_child_sort_order(int(parent.id), index),
        content=request.content,
        priority="P4",
        due_date=None,
        is_done=False,
        is_urgent=False,
    )
    db.add(child)
    reopened_ids = hierarchy.reopen_completed_ancestors(
        db,
        user_id=current_user.id,
        todo_id=int(parent.id),
        index=index,
        include_self=True,
    )
    db.flush()
    if add_to_today:
        plan_svc.add_todo_subtree_to_today(
            db,
            user_id=current_user.id,
            todo_id=root_id,
            source=plan_svc.SOURCE_MANUAL,
        )
    db.commit()
    best_effort_sync_todo_ids(
        db,
        user_id=current_user.id,
        todo_ids=[int(child.id), *reopened_ids],
    )
    fresh = hierarchy.load_index(db, current_user.id)
    return build_todo_response(fresh.by_id[int(child.id)], hierarchy.metadata_for(int(child.id), fresh))


@router.put("/{parent_id}/children/order", response_model=TodoChildrenOrderResponse)
def reorder_children(
    parent_id: int,
    request: TodoChildrenOrderRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    index = hierarchy.load_index(db, current_user.id, for_update=True)
    if int(parent_id) not in index.by_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="小要事不存在")

    ordered = [int(item) for item in request.ordered_child_ids]
    current = list(index.children.get(int(parent_id), []))
    if len(ordered) != len(set(ordered)) or set(ordered) != set(current):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": hierarchy.CODE_ORDER_STALE, "message": "子任务列表已变化，请刷新后重试"},
        )
    for position, child_id in enumerate(ordered):
        index.by_id[child_id].sort_order = position
    db.commit()
    return TodoChildrenOrderResponse(parent_id=int(parent_id), ordered_child_ids=ordered)


@router.get("/today-plan", response_model=TodayPlanResponse)
def get_today_plan(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    today = today_local()
    plan_items = plan_svc.list_today_plan_items(db, user_id=current_user.id, plan_date=today)
    index = hierarchy.load_index(db, current_user.id)
    meta = hierarchy.metadata_map(index)
    built: List[TodayPlanItemResponse] = []
    for item in plan_items:
        todo = index.by_id.get(int(item.todo_id))
        if todo is None:
            continue
        built.append(
            TodayPlanItemResponse(
                plan_item_id=int(item.id),
                plan_date=item.plan_date,
                source=item.source,
                status=item.status,
                todo=build_todo_response(todo, meta[int(todo.id)]),
            )
        )
    built.sort(key=lambda row: todo_sort_key(index.by_id[row.todo.id]))
    counted = [row for row in built if row.todo.parent_id is None]
    completed = sum(
        1 for row in counted if row.todo.is_done or row.status == plan_svc.PLAN_COMPLETED
    )
    total = len(counted)
    rate = (completed / total * 100.0) if total else 0.0
    return TodayPlanResponse(
        plan_date=today,
        items=built,
        completed=completed,
        total=total,
        completion_rate=round(rate, 1),
    )


@router.get("/today-plan/urgent-preview", response_model=UrgentPreviewResponse)
def urgent_preview(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    today = today_local()
    rows = plan_svc.list_urgent_candidates(db, user_id=current_user.id, plan_date=today)
    index = hierarchy.load_index(db, current_user.id)
    meta = hierarchy.metadata_map(index)
    items = [
        UrgentPreviewItem(
            todo=build_todo_response(todo, meta[int(todo.id)]),
            reasons=reasons,
            past_active_plan_date=past_date,
            affected_descendant_count=int(meta[int(todo.id)]["descendant_count"]),
        )
        for todo, reasons, past_date in rows
    ]
    items.sort(key=lambda row: todo_sort_key(index.by_id[row.todo.id]))
    return UrgentPreviewResponse(plan_date=today, items=items)


def _review_http_error(exc: Exception) -> HTTPException:
    code = getattr(exc, "code", None) or "REVIEW_REJECTED"
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"code": code, "message": str(exc)},
    )


@router.post("/today-plan/urgent-confirm", response_model=UrgentConfirmResponse)
def urgent_confirm(
    request: UrgentConfirmRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    decisions = [
        (item.todo_id, item.action, item.expected_descendant_count)
        for item in request.decisions
    ]
    delete_index = hierarchy.load_index(db, current_user.id)
    deleted_tree_ids = [
        node_id
        for item in request.decisions
        if item.action == "delete" and int(item.todo_id) in delete_index.by_id
        for node_id in hierarchy.subtree_ids(int(item.todo_id), delete_index)
    ]
    try:
        kept, deleted = plan_svc.confirm_urgent_decisions(
            db,
            user_id=current_user.id,
            plan_date=request.plan_date,
            decisions=decisions,
        )
    except (plan_svc.ReviewStaleError, plan_svc.ReviewRejectedError) as exc:
        raise _review_http_error(exc) from exc
    delete_todo_knowledge(db, user_id=current_user.id, todo_ids=deleted_tree_ids)
    db.commit()
    return UrgentConfirmResponse(
        plan_date=request.plan_date,
        kept_todo_ids=kept,
        deleted_todo_ids=deleted,
    )


@router.get("/today-plan/rollover-preview", response_model=RolloverPreviewResponse)
def rollover_preview(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    today = today_local()
    index = hierarchy.load_index(db, current_user.id)
    urgent_roots = {
        int(todo.id)
        for todo, _, _ in plan_svc.list_urgent_candidates(
            db, user_id=current_user.id, plan_date=today
        )
    }
    urgent_tree_ids = {
        item
        for root_id in urgent_roots
        for item in hierarchy.subtree_ids(root_id, index)
    }
    rows = plan_svc.list_rollover_candidates(
        db,
        user_id=current_user.id,
        plan_date=today,
        exclude_todo_ids=urgent_tree_ids,
    )
    meta = hierarchy.metadata_map(index)
    items = [
        RolloverPreviewItem(
            todo=build_todo_response(todo, meta[int(todo.id)]),
            past_plan_date=plan_item.plan_date,
            past_plan_item_id=int(plan_item.id),
            affected_descendant_count=int(meta[int(todo.id)]["descendant_count"]),
        )
        for todo, plan_item in rows
    ]
    items.sort(key=lambda row: todo_sort_key(index.by_id[row.todo.id]))
    return RolloverPreviewResponse(plan_date=today, items=items)


@router.post("/today-plan/rollover-confirm", response_model=RolloverConfirmResponse)
def rollover_confirm(
    request: RolloverConfirmRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        added, returned = plan_svc.confirm_rollover(
            db,
            user_id=current_user.id,
            plan_date=request.plan_date,
            reviewed_todo_ids=request.reviewed_todo_ids,
            selected_todo_ids=request.selected_todo_ids,
            expected_descendant_counts=request.expected_descendant_counts,
        )
    except (plan_svc.ReviewStaleError, plan_svc.ReviewRejectedError) as exc:
        raise _review_http_error(exc) from exc
    db.commit()
    return RolloverConfirmResponse(
        plan_date=request.plan_date,
        added_todo_ids=added,
        returned_todo_ids=returned,
    )


@router.post("/{todo_id}/today-plan", response_model=TodayPlanItemResponse)
def add_to_today_plan(
    todo_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    index = hierarchy.load_index(db, current_user.id)
    if int(todo_id) not in index.by_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="小要事不存在")
    root_id = hierarchy.family_root_id(int(todo_id), index)
    item = plan_svc.add_todo_subtree_to_today(
        db,
        user_id=current_user.id,
        todo_id=root_id,
        source=plan_svc.SOURCE_MANUAL,
    )
    db.commit()
    index = hierarchy.load_index(db, current_user.id)
    todo = index.by_id[root_id]
    return TodayPlanItemResponse(
        plan_item_id=int(item.id),
        plan_date=item.plan_date,
        source=item.source,
        status=item.status,
        todo=build_todo_response(todo, hierarchy.metadata_for(root_id, index)),
    )


@router.delete("/{todo_id}/today-plan", status_code=status.HTTP_204_NO_CONTENT)
def remove_from_today_plan(
    todo_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    index = hierarchy.load_index(db, current_user.id)
    if int(todo_id) not in index.by_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="小要事不存在")
    root_id = hierarchy.family_root_id(int(todo_id), index)
    plan_svc.remove_todo_subtree_from_today(db, user_id=current_user.id, todo_id=root_id)
    db.commit()


@router.patch("/{todo_id}", response_model=TodoResponse)
def update_todo(
    todo_id: int,
    request: TodoUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    index = hierarchy.load_index(db, current_user.id, for_update=True)
    todo = index.by_id.get(int(todo_id))
    if todo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="小要事不存在")

    fields = _model_fields_set(request)
    if getattr(todo, "parent_id", None) is not None and fields.intersection(
        {"due_date", "priority", "is_urgent"}
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "TODO_CHILD_STEP_ONLY", "message": "子任务只支持正文、完成状态和顺序"},
        )
    if request.content is not None:
        todo.content = request.content
    if "due_date" in fields:
        todo.due_date = request.due_date
    if request.priority is not None:
        todo.priority = request.priority
    if request.is_urgent is not None:
        todo.is_urgent = bool(request.is_urgent)

    reopened_ids: List[int] = []
    if request.is_done is not None:
        if request.is_done:
            incomplete = hierarchy.incomplete_descendant_ids(todo_id, index)
            if incomplete:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": hierarchy.CODE_DESCENDANTS_INCOMPLETE,
                        "message": "请先完成所有子任务",
                        "incomplete_descendant_count": len(incomplete),
                    },
                )
            todo.is_done = True
            todo.completed_at = now_local()
            if "completion_note" in fields:
                todo.completion_note = _normalize_completion_note(request.completion_note)
        else:
            todo.is_done = False
            todo.completed_at = None
            todo.completion_note = None
            if getattr(todo, "parent_id", None) is not None:
                reopened_ids = hierarchy.reopen_completed_ancestors(
                    db,
                    user_id=current_user.id,
                    todo_id=todo_id,
                    index=index,
                )
        plan_svc.sync_plan_on_todo_done_change(
            db,
            user_id=current_user.id,
            todo_id=todo_id,
            is_done=bool(request.is_done),
        )
    elif "completion_note" in fields:
        if not todo.is_done:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="仅已完成的小要事可填写完成批注",
            )
        todo.completion_note = _normalize_completion_note(request.completion_note)

    db.commit()
    best_effort_sync_todo_ids(
        db,
        user_id=current_user.id,
        todo_ids=[int(todo_id), *reopened_ids],
    )
    fresh = hierarchy.load_index(db, current_user.id)
    return build_todo_response(fresh.by_id[todo_id], hierarchy.metadata_for(todo_id, fresh))


@router.delete("/{todo_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_todo(
    todo_id: int,
    expected_descendant_count: Optional[int] = Query(default=None, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    index = hierarchy.load_index(db, current_user.id, for_update=True)
    todo = index.by_id.get(int(todo_id))
    if todo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="小要事不存在")
    try:
        hierarchy.assert_expected_descendant_count(
            todo_id,
            expected_descendant_count,
            index,
        )
    except hierarchy.TodoHierarchyError as exc:
        raise _hierarchy_http_error(exc) from exc
    tree_ids = hierarchy.subtree_ids(todo_id, index)
    delete_todo_knowledge(db, user_id=current_user.id, todo_ids=tree_ids)
    db.delete(todo)
    db.commit()


@router.get("/stats/weekly", response_model=WeeklyStatsResponse)
def get_weekly_stats(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    today = today_local()
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=6)
    week_start_dt = datetime.combine(week_start, datetime.min.time())
    week_end_dt = datetime.combine(week_end, datetime.max.time())

    query = db.query(Todo).filter(
        Todo.user_id == current_user.id,
        Todo.parent_id.is_(None),
        Todo.created_at >= week_start_dt,
        Todo.created_at <= week_end_dt,
    )
    total = query.count()
    completed_count = query.filter(Todo.is_done.is_(True)).count()
    pending = total - completed_count
    completion_rate = (completed_count / total * 100) if total else 0.0
    return WeeklyStatsResponse(
        week_start=week_start,
        week_end=week_end,
        total=total,
        completed=completed_count,
        pending=pending,
        completion_rate=round(completion_rate, 1),
    )