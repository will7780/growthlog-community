"""
标签相关的 Pydantic 模型
"""
from pydantic import BaseModel, Field
from typing import Optional


class LabelResponse(BaseModel):
    """标签响应"""
    code: str
    name: str
    sort_order: int

    class Config:
        from_attributes = True


class UserLabelItem(BaseModel):
    """用户标签项（个人中心用）"""
    code: str
    name: str
    sort_order: int
    is_system: bool = Field(description="是否为系统预设标签")
    can_delete: bool = Field(description="是否可以删除")
    entry_count: int = Field(default=0, description="该标签下的记录数")


class UserLabelsResponse(BaseModel):
    """用户所有标签响应"""
    labels: list[UserLabelItem]
    total_count: int


class CreateLabelRequest(BaseModel):
    """创建标签请求"""
    name: str = Field(..., min_length=2, max_length=64, description="标签名称")


class UpdateLabelRequest(BaseModel):
    """更新标签请求"""
    name: Optional[str] = Field(None, min_length=2, max_length=64, description="标签名称")
    sort_order: Optional[int] = Field(None, ge=0, description="排序顺序")


class DeleteLabelRequest(BaseModel):
    """删除标签请求"""
    target_code: Optional[str] = Field(None, description="归档目标标签 code（有记录时必填）")
