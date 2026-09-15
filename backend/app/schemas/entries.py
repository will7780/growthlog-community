"""
记录相关的 Pydantic 模型
"""
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime
from app.schemas.attachments import AttachmentResponse


class EntryCreateRequest(BaseModel):
    """创建记录请求"""
    label_code: str = Field(..., min_length=1, max_length=32)
    content: str = Field(..., min_length=1, max_length=10000)
    parent_id: Optional[int] = Field(default=None, ge=1, description="父记录ID，为NULL时创建顶级记录")


class EntryUpdateRequest(BaseModel):
    """更新记录请求"""
    content: Optional[str] = Field(default=None, min_length=1, max_length=10000)


class EntryResponse(BaseModel):
    """记录响应"""
    id: int
    user_id: int
    label_code: str
    label_name: str
    content: str
    parent_id: Optional[int] = None
    created_at: datetime
    updated_at: Optional[datetime] = None
    attachments: List[AttachmentResponse] = []

    class Config:
        from_attributes = True


class EntryWithChildrenResponse(BaseModel):
    """带子记录的记录响应"""
    id: int
    user_id: int
    label_code: str
    label_name: str
    content: str
    parent_id: Optional[int] = None
    created_at: datetime
    updated_at: Optional[datetime] = None
    attachments: List[AttachmentResponse] = []
    children: List["EntryWithChildrenResponse"] = []

    class Config:
        from_attributes = True


class EntryListResponse(BaseModel):
    """记录列表响应"""
    items: List[EntryWithChildrenResponse]
    total: int
    limit: int
    offset: int


# 解决循环引用
EntryWithChildrenResponse.model_rebuild()
