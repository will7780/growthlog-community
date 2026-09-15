"""
认证相关的 Pydantic 模型
"""
from __future__ import annotations
from pydantic import BaseModel, Field
from datetime import datetime


class LoginRequest(BaseModel):
    """登录请求"""
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1)


class UserResponse(BaseModel):
    """用户信息响应（永不包含 password_hash）"""
    id: int
    username: str
    is_active: bool
    is_admin: bool = False
    can_edit_delete_own_entries: bool = False
    created_at: datetime

    class Config:
        from_attributes = True


class LoginResponse(BaseModel):
    """登录响应"""
    access_token: str
    token_type: str = "bearer"
    user: UserResponse
