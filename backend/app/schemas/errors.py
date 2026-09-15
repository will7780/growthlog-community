"""
错误响应相关的 Pydantic 模型
"""
from pydantic import BaseModel
from typing import Optional


class ErrorDetail(BaseModel):
    """错误详情"""
    code: str
    message: str
    details: Optional[dict] = None


class ErrorResponse(BaseModel):
    """统一错误响应"""
    error: ErrorDetail
