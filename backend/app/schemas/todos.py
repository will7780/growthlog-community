"""小要事及 Today Plan API contracts."""
from datetime import date, datetime
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

TodoPriority = Literal["P0", "P1", "P2", "P3", "P4"]
PlanSource = Literal["manual", "rollover"]
PlanStatus = Literal["active", "completed", "carried", "returned"]
UrgentAction = Literal["keep", "delete"]


class TodoCreateRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=500)
    due_date: Optional[date] = Field(default=None)
    priority: TodoPriority = Field(default="P4")
    is_urgent: bool = Field(default=False)
    add_to_today: bool = Field(default=False)


class TodoChildCreateRequest(BaseModel):
    """Omitted settings inherit from the parent; explicit null clears due date."""

    content: str = Field(..., min_length=1, max_length=500)
    due_date: Optional[date] = Field(default=None)
    priority: Optional[TodoPriority] = Field(default=None)
    is_urgent: Optional[bool] = Field(default=None)
    add_to_today: Optional[bool] = Field(default=None)


class TodoUpdateRequest(BaseModel):
    content: Optional[str] = Field(default=None, min_length=1, max_length=500)
    due_date: Optional[date] = Field(default=None)
    is_done: Optional[bool] = Field(default=None)
    priority: Optional[TodoPriority] = Field(default=None)
    is_urgent: Optional[bool] = Field(default=None)
    completion_note: Optional[str] = Field(default=None, max_length=1000)


class TodoResponse(BaseModel):
    id: int
    content: str
    priority: TodoPriority
    due_date: Optional[date] = None
    is_done: bool
    is_urgent: bool = False
    is_expiring_soon: bool
    is_overdue: bool = False
    completed_at: Optional[datetime] = None
    completion_note: Optional[str] = None
    created_at: datetime
    parent_id: Optional[int] = None
    sort_order: Optional[int] = None
    depth: int = 0
    family_root_id: int
    ancestor_titles: List[str] = Field(default_factory=list)
    direct_child_count: int = 0
    descendant_count: int = 0
    incomplete_descendant_count: int = 0

    @model_validator(mode="before")
    @classmethod
    def default_root_to_self(cls, value):
        """Keep legacy direct constructors compatible while routes send full metadata."""
        if isinstance(value, dict) and value.get("family_root_id") is None and value.get("id") is not None:
            value = dict(value)
            value["family_root_id"] = value["id"]
        return value

    class Config:
        from_attributes = True


class TodoTreeNode(TodoResponse):
    children: List["TodoTreeNode"] = Field(default_factory=list)


class TodoListResponse(BaseModel):
    items: List[TodoResponse]
    total: int
    limit: int
    offset: int


class TodoTreeListResponse(BaseModel):
    items: List[TodoTreeNode]
    total: int
    limit: int
    offset: int


class TodoChildrenOrderRequest(BaseModel):
    ordered_child_ids: List[int] = Field(default_factory=list)


class TodoChildrenOrderResponse(BaseModel):
    parent_id: int
    ordered_child_ids: List[int]


class WeeklyStatsResponse(BaseModel):
    week_start: date
    week_end: date
    total: int
    completed: int
    pending: int
    completion_rate: float


class TodayPlanItemResponse(BaseModel):
    plan_item_id: int
    plan_date: date
    source: PlanSource
    status: PlanStatus
    todo: TodoResponse


class TodayPlanResponse(BaseModel):
    plan_date: date
    items: List[TodayPlanItemResponse]
    completed: int
    total: int
    completion_rate: float


class UrgentPreviewItem(BaseModel):
    todo: TodoResponse
    reasons: List[str]
    past_active_plan_date: Optional[date] = None
    affected_descendant_count: int = 0


class UrgentPreviewResponse(BaseModel):
    plan_date: date
    items: List[UrgentPreviewItem]


class UrgentDecision(BaseModel):
    todo_id: int
    action: UrgentAction
    expected_descendant_count: Optional[int] = Field(default=None, ge=0)


class UrgentConfirmRequest(BaseModel):
    plan_date: date
    decisions: List[UrgentDecision] = Field(default_factory=list)


class UrgentConfirmResponse(BaseModel):
    plan_date: date
    kept_todo_ids: List[int]
    deleted_todo_ids: List[int]


class RolloverPreviewItem(BaseModel):
    todo: TodoResponse
    past_plan_date: date
    past_plan_item_id: int
    affected_descendant_count: int = 0


class RolloverPreviewResponse(BaseModel):
    plan_date: date
    items: List[RolloverPreviewItem]


class RolloverConfirmRequest(BaseModel):
    plan_date: date
    reviewed_todo_ids: List[int] = Field(default_factory=list)
    selected_todo_ids: List[int] = Field(default_factory=list)
    expected_descendant_counts: Dict[int, int] = Field(default_factory=dict)


class RolloverConfirmResponse(BaseModel):
    plan_date: date
    added_todo_ids: List[int]
    returned_todo_ids: List[int]