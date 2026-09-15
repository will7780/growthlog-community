"""
标签业务服务
提供标签的 CRUD 操作和归档逻辑
"""
import time
import random
import logging
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session
from sqlalchemy import func

from app.models import EntryLabel, Entry

logger = logging.getLogger(__name__)

# 预设不可删除的标签
SYSTEM_PROTECTED_LABELS = {"todo"}  # 小要事不可删除

# 标签数量限制
MIN_LABELS = 2
MAX_LABELS = 7


def generate_unique_code() -> str:
    """生成唯一的标签 code"""
    timestamp = int(time.time() * 1000)
    random_suffix = ''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=4))
    return f"custom_{timestamp}_{random_suffix}"


def get_user_labels(db: Session, user_id: int) -> List[dict]:
    """
    获取用户的所有标签（含系统预设标签）
    标签来源：系统预设标签 + 用户自定义标签
    """
    labels = db.query(EntryLabel).filter(
        (EntryLabel.user_id == None) | (EntryLabel.user_id == user_id),
        EntryLabel.is_active == True
    ).order_by(EntryLabel.sort_order.asc()).all()

    result = []
    for label in labels:
        # 统计该用户此标签下的记录数（只统计用户自己的记录）
        entry_count = db.query(func.count(Entry.id)).filter(
            Entry.label_code == label.code,
            Entry.user_id == user_id
        ).scalar() or 0

        can_delete = label.code not in SYSTEM_PROTECTED_LABELS

        result.append({
            "code": label.code,
            "name": label.name,
            "sort_order": label.sort_order,
            "is_system": label.user_id is None,
            "can_delete": can_delete,
            "entry_count": int(entry_count)
        })

    return result


def get_label_entry_counts(db: Session, user_id: int) -> dict:
    """
    获取用户各标签的记录数
    """
    results = db.query(
        Entry.label_code,
        func.count(Entry.id).label("count")
    ).filter(
        Entry.user_id == user_id
    ).group_by(Entry.label_code).all()

    return {r.label_code: int(r.count) for r in results}


def check_label_name_exists(db: Session, user_id: int, name: str, exclude_code: Optional[str] = None) -> bool:
    """
    检查标签名称是否已存在（同名不可重复）
    """
    query = db.query(EntryLabel).filter(
        EntryLabel.name == name,
        EntryLabel.is_active == True,
        (EntryLabel.user_id == None) | (EntryLabel.user_id == user_id)
    )
    if exclude_code:
        query = query.filter(EntryLabel.code != exclude_code)
    return query.first() is not None


def create_label(db: Session, user_id: int, name: str) -> Tuple[EntryLabel, Optional[str]]:
    """
    创建新标签

    Returns:
        (label, error_code)
        成功时 error_code 为 None
    """
    # 检查数量限制
    current_count = db.query(EntryLabel).filter(
        (EntryLabel.user_id == None) | (EntryLabel.user_id == user_id),
        EntryLabel.is_active == True
    ).count()

    if current_count >= MAX_LABELS:
        return None, "LABEL_COUNT_LIMIT"

    # 检查名称重复
    if check_label_name_exists(db, user_id, name):
        return None, "LABEL_NAME_DUPLICATED"

    # 检查名称长度
    if len(name) < 2:
        return None, "LABEL_NAME_TOO_SHORT"

    # 获取最大 sort_order
    max_order = db.query(func.max(EntryLabel.sort_order)).filter(
        (EntryLabel.user_id == None) | (EntryLabel.user_id == user_id)
    ).scalar() or 0

    # 生成唯一 code
    code = generate_unique_code()
    while db.query(EntryLabel).filter(EntryLabel.code == code).first():
        code = generate_unique_code()

    # 创建标签
    label = EntryLabel(
        code=code,
        name=name,
        sort_order=max_order + 1,
        user_id=user_id,
        is_active=True
    )
    db.add(label)
    db.commit()
    db.refresh(label)

    return label, None


def update_label(db: Session, user_id: int, code: str, name: Optional[str] = None, sort_order: Optional[int] = None) -> Tuple[Optional[EntryLabel], Optional[str]]:
    """
    更新标签

    Returns:
        (label, error_code)
        成功时 error_code 为 None
    """
    label = db.query(EntryLabel).filter(
        EntryLabel.code == code,
        EntryLabel.is_active == True
    ).first()

    if not label:
        return None, "NOT_FOUND"

    # 检查是否有权限修改（系统标签允许，小要事不可修改）
    if label.code in SYSTEM_PROTECTED_LABELS:
        return None, "CANNOT_MODIFY_SYSTEM_LABEL"

    # 检查名称重复（排除自己）
    if name and name != label.name:
        if check_label_name_exists(db, user_id, name, exclude_code=code):
            return None, "LABEL_NAME_DUPLICATED"
        if len(name) < 2:
            return None, "LABEL_NAME_TOO_SHORT"
        label.name = name

    if sort_order is not None:
        label.sort_order = sort_order

    db.commit()
    db.refresh(label)

    return label, None


def delete_label(db: Session, user_id: int, code: str, target_code: Optional[str] = None) -> Tuple[bool, Optional[str]]:
    """
    删除标签

    Args:
        user_id: 当前用户 ID
        code: 要删除的标签 code
        target_code: 归档目标标签 code（有记录时必填）

    Returns:
        (success, error_code)
        成功时 error_code 为 None
    """
    label = db.query(EntryLabel).filter(
        EntryLabel.code == code,
        EntryLabel.is_active == True
    ).first()

    if not label:
        return False, "NOT_FOUND"

    # 检查是否可以删除
    if code in SYSTEM_PROTECTED_LABELS:
        return False, "CANNOT_DELETE_SYSTEM_LABEL"

    # 统计该用户此标签下的记录数
    entry_count = db.query(func.count(Entry.id)).filter(
        Entry.label_code == code,
        Entry.user_id == user_id
    ).scalar() or 0

    # 有记录时必须指定归档目标
    if entry_count > 0 and not target_code:
        return False, "TARGET_REQUIRED"

    # 归档目标不能是自己
    if target_code == code:
        return False, "TARGET_CANNOT_BE_SELF"

    # 验证归档目标存在
    if target_code:
        target_label = db.query(EntryLabel).filter(
            EntryLabel.code == target_code,
            EntryLabel.is_active == True
        ).first()
        if not target_label:
            return False, "TARGET_NOT_FOUND"

    # 检查删除后剩余标签数
    remaining_count = db.query(EntryLabel).filter(
        (EntryLabel.user_id == None) | (EntryLabel.user_id == user_id),
        EntryLabel.is_active == True,
        EntryLabel.code != code
    ).count()

    if remaining_count < MIN_LABELS:
        return False, "LABEL_COUNT_BELOW_MIN"

    # 执行归档
    if entry_count > 0 and target_code:
        db.query(Entry).filter(
            Entry.label_code == code,
            Entry.user_id == user_id
        ).update({Entry.label_code: target_code})

    # 软删除标签
    label.is_active = False
    db.commit()

    return True, None
