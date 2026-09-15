"""
标签路由
GET /api/labels - 获取所有激活的标签列表（带进程内 TTL 缓存）
POST/PUT/DELETE /api/labels/* - 个人中心标签管理
"""
import time
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Optional
from app.database import get_db
from app.models import EntryLabel
from app.schemas import LabelResponse, UserLabelsResponse, UserLabelItem
from app.schemas.labels import (
    CreateLabelRequest,
    UpdateLabelRequest,
    DeleteLabelRequest
)
from app.auth import get_current_user, User
from app.services import label_service

router = APIRouter()

_labels_cache: dict[int, tuple[float, List[LabelResponse]]] = {}
_CACHE_TTL_SECONDS = 300  # 5 分钟


def _invalidate_cache():
    """清除标签缓存"""
    _labels_cache.clear()


@router.get("", response_model=List[LabelResponse])
def get_labels(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    获取所有激活的标签列表
    按 sort_order 升序排列，结果缓存 5 分钟
    """
    now = time.monotonic()
    user_id = int(current_user.id)
    cached = _labels_cache.get(user_id)
    if cached is not None and (now - cached[0]) < _CACHE_TTL_SECONDS:
        return cached[1]

    labels = db.query(EntryLabel).filter(
        EntryLabel.is_active == True,
        (EntryLabel.user_id.is_(None)) | (EntryLabel.user_id == user_id),
    ).order_by(EntryLabel.sort_order.asc()).all()

    result = [
        LabelResponse(
            code=label.code,
            name=label.name,
            sort_order=label.sort_order
        )
        for label in labels
    ]

    if len(_labels_cache) >= 1024:
        _invalidate_cache()
    _labels_cache[user_id] = (now, result)

    return result


@router.get("/me", response_model=UserLabelsResponse)
def get_user_labels(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    获取当前用户的标签列表（个人中心用）
    返回系统预设标签 + 用户自定义标签
    """
    labels = label_service.get_user_labels(db, current_user.id)
    return UserLabelsResponse(
        labels=[UserLabelItem(**label) for label in labels],
        total_count=len(labels)
    )


@router.post("", response_model=UserLabelItem, status_code=status.HTTP_201_CREATED)
def create_label(
    req: CreateLabelRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    创建新标签
    """
    label, err = label_service.create_label(db, current_user.id, req.name)
    if err:
        if err == "LABEL_COUNT_LIMIT":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="标签数量已达上限（最多7个）"
            )
        elif err == "LABEL_NAME_DUPLICATED":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="标签名称已存在"
            )
        elif err == "LABEL_NAME_TOO_SHORT":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="标签名称太短（至少2个字符）"
            )
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="创建失败")

    _invalidate_cache()
    return UserLabelItem(
        code=label.code,
        name=label.name,
        sort_order=label.sort_order,
        is_system=False,
        can_delete=True,
        entry_count=0
    )


@router.put("/{code}", response_model=UserLabelItem)
def update_label(
    code: str,
    req: UpdateLabelRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    更新标签
    """
    label, err = label_service.update_label(
        db, current_user.id, code,
        name=req.name,
        sort_order=req.sort_order
    )
    if err:
        if err == "NOT_FOUND":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="标签不存在")
        elif err == "CANNOT_MODIFY_SYSTEM_LABEL":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="系统标签不可修改")
        elif err == "LABEL_NAME_DUPLICATED":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="标签名称已存在")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="更新失败")

    _invalidate_cache()
    entry_count = label_service.get_label_entry_counts(db, current_user.id).get(code, 0)
    return UserLabelItem(
        code=label.code,
        name=label.name,
        sort_order=label.sort_order,
        is_system=label.user_id is None,
        can_delete=label.code not in label_service.SYSTEM_PROTECTED_LABELS,
        entry_count=entry_count
    )


@router.delete("/{code}", status_code=status.HTTP_204_NO_CONTENT)
def delete_label(
    code: str,
    req: DeleteLabelRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    删除标签
    """
    success, err = label_service.delete_label(
        db, current_user.id, code,
        target_code=req.target_code
    )
    if not success:
        if err == "NOT_FOUND":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="标签不存在")
        elif err == "CANNOT_DELETE_SYSTEM_LABEL":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="系统标签不可删除")
        elif err == "TARGET_REQUIRED":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="该标签下有记录，请先选择归档目标")
        elif err == "TARGET_CANNOT_BE_SELF":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="归档目标不能是自己")
        elif err == "TARGET_NOT_FOUND":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="归档目标不存在")
        elif err == "LABEL_COUNT_BELOW_MIN":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="删除后标签数量不能少于2个")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="删除失败")

    _invalidate_cache()
    return None
