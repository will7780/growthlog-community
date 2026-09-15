"""
管理接口：
- 用户管理：仅 JWT 管理员（is_admin），禁止 localhost / ADMIN_TOKEN 绕过
- 向量索引运维：保留本机或 ADMIN_TOKEN（不暴露给前端）
"""
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import get_current_admin_user, get_password_hash
from app.config import settings
from app.database import get_db
from app.models import User
from app.schemas.auth import UserResponse

router = APIRouter()
_ops_security = HTTPBearer(auto_error=False)
logger = logging.getLogger(__name__)


class AdminCreateUserRequest(BaseModel):
    """创建普通用户（默认非管理员）。"""

    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)


class AdminUpdateUserRequest(BaseModel):
    is_active: Optional[bool] = None
    is_admin: Optional[bool] = None
    can_edit_delete_own_entries: Optional[bool] = None


class AdminResetPasswordRequest(BaseModel):
    new_password: str = Field(..., min_length=1, max_length=128)


class AdminUserListResponse(BaseModel):
    items: List[UserResponse]
    total: int


def _user_response(user: User) -> UserResponse:
    return UserResponse(
        id=int(user.id),
        username=user.username,
        is_active=bool(user.is_active),
        is_admin=bool(getattr(user, "is_admin", False)),
        can_edit_delete_own_entries=bool(
            getattr(user, "can_edit_delete_own_entries", False)
        ),
        created_at=user.created_at,
    )


def _audit(
    actor_id: int,
    action: str,
    target_id: int | None,
    result: str,
    *,
    before: str | None = None,
    after: str | None = None,
) -> None:
    """审计：actor/action/target/before/after/result；禁止密码、token、正文。"""
    logger.info(
        "admin_audit actor_id=%s action=%s target_id=%s before=%s after=%s result=%s",
        actor_id,
        action,
        target_id,
        before,
        after,
        result,
    )


def _allow_ops_admin(
    request: Request, credentials: HTTPAuthorizationCredentials | None
) -> bool:
    """运维通道：本机或 Bearer ADMIN_TOKEN。不得用于用户管理。"""
    host = (request.client and request.client.host) or ""
    if host in ("127.0.0.1", "::1"):
        return True
    if settings.admin_token and credentials and credentials.credentials == settings.admin_token:
        return True
    return False


async def verify_ops_admin(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_ops_security),
):
    if not _allow_ops_admin(request, credentials):
        raise HTTPException(status_code=403, detail="仅允许本机或有效 ADMIN_TOKEN 调用")
    return True


def _lock_target_and_active_admins(db: Session, target_id: int) -> list[User]:
    """
    在同一事务中按 id 升序锁定目标用户与当前有效管理员，避免死锁与最后管理员竞态。
    """
    return (
        db.query(User)
        .filter(
            or_(
                User.id == target_id,
                and_(User.is_admin.is_(True), User.is_active.is_(True)),
            )
        )
        .order_by(User.id.asc())
        .with_for_update()
        .all()
    )


def _validate_user_mutation(
    actor: User,
    target: User,
    locked_rows: list[User],
    is_active: bool | None,
    is_admin: bool | None,
) -> None:
    """基于锁定后的最新行状态校验自我操作与最后有效管理员保护。"""
    if target.id == actor.id:
        if is_active is False:
            raise HTTPException(status_code=403, detail="不能停用自己的账号")
        if is_admin is False:
            raise HTTPException(status_code=403, detail="不能撤销自己的管理员权限")

    would_disable = is_active is False and bool(target.is_active)
    would_demote = is_admin is False and bool(target.is_admin)

    if (would_disable or would_demote) and bool(target.is_admin) and bool(target.is_active):
        other_active_admins = [
            u
            for u in locked_rows
            if int(u.id) != int(target.id)
            and bool(u.is_admin)
            and bool(u.is_active)
        ]
        if len(other_active_admins) == 0:
            raise HTTPException(
                status_code=403,
                detail="不能停用或降级最后一个有效管理员",
            )


@router.get("/users", response_model=AdminUserListResponse)
def list_users(
    username: Optional[str] = Query(None, description="按用户名模糊搜索"),
    admin_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
):
    q = db.query(User).order_by(User.id.asc())
    if username and username.strip():
        q = q.filter(User.username.contains(username.strip()))
    users = q.all()
    _audit(int(admin_user.id), "list_users", None, "ok")
    return AdminUserListResponse(
        items=[_user_response(u) for u in users],
        total=len(users),
    )


@router.post("/users", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def create_user(
    body: AdminCreateUserRequest,
    admin_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
):
    """创建用户：始终为普通用户（is_admin=false）。"""
    username = body.username.strip()
    if not username:
        raise HTTPException(status_code=400, detail="用户名不能为空")
    if not body.password.strip():
        raise HTTPException(status_code=400, detail="密码不能为空")

    existing = db.query(User).filter(User.username == username).first()
    if existing:
        _audit(int(admin_user.id), "create_user", None, "conflict")
        raise HTTPException(status_code=409, detail="用户名已存在")

    user = User(
        username=username,
        password_hash=get_password_hash(body.password),
        is_active=True,
        is_admin=False,
        can_edit_delete_own_entries=False,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        _audit(int(admin_user.id), "create_user", None, "conflict")
        raise HTTPException(status_code=409, detail="用户名已存在")
    except Exception:
        db.rollback()
        _audit(int(admin_user.id), "create_user", None, "error")
        raise

    db.refresh(user)
    _audit(int(admin_user.id), "create_user", int(user.id), "ok")
    return _user_response(user)


@router.patch("/users/{user_id}", response_model=UserResponse)
def update_user(
    body: AdminUpdateUserRequest,
    user_id: int = Path(..., description="用户 ID"),
    admin_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
):
    if (
        body.is_active is None
        and body.is_admin is None
        and body.can_edit_delete_own_entries is None
    ):
        raise HTTPException(status_code=400, detail="请至少提供一个更新字段")

    before: str | None = None
    after: str | None = None
    try:
        locked = _lock_target_and_active_admins(db, user_id)
        target = next((u for u in locked if int(u.id) == int(user_id)), None)
        if target is None:
            db.rollback()
            _audit(int(admin_user.id), "update_user", user_id, "denied:404")
            raise HTTPException(status_code=404, detail="用户不存在")

        try:
            _validate_user_mutation(
                admin_user, target, locked, body.is_active, body.is_admin
            )
        except HTTPException as exc:
            db.rollback()
            _audit(
                int(admin_user.id),
                "update_user",
                user_id,
                f"denied:{exc.status_code}",
            )
            raise

        before = (
            f"is_active={bool(target.is_active)};"
            f"is_admin={bool(target.is_admin)};"
            f"can_edit_delete_own_entries="
            f"{bool(getattr(target, 'can_edit_delete_own_entries', False))}"
        )
        if body.is_active is not None:
            target.is_active = body.is_active
        if body.is_admin is not None:
            target.is_admin = body.is_admin
        if body.can_edit_delete_own_entries is not None:
            target.can_edit_delete_own_entries = body.can_edit_delete_own_entries

        after = (
            f"is_active={bool(target.is_active)};"
            f"is_admin={bool(target.is_admin)};"
            f"can_edit_delete_own_entries="
            f"{bool(getattr(target, 'can_edit_delete_own_entries', False))}"
        )
        db.commit()
    except HTTPException:
        raise
    except Exception:
        db.rollback()
        _audit(int(admin_user.id), "update_user", user_id, "error")
        raise

    # 提交后重新查询，避免继续使用 rollback 前可能陈旧的实例语义
    fresh = db.query(User).filter(User.id == user_id).first()
    if fresh is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    _audit(
        int(admin_user.id),
        "update_user",
        user_id,
        "ok",
        before=before,
        after=after,
    )
    return _user_response(fresh)


@router.post("/users/{user_id}/reset-password", response_model=UserResponse)
def reset_password(
    body: AdminResetPasswordRequest,
    user_id: int = Path(..., description="用户 ID"),
    admin_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
):
    if not body.new_password.strip():
        raise HTTPException(status_code=400, detail="密码不能为空")

    try:
        target = (
            db.query(User)
            .filter(User.id == user_id)
            .with_for_update()
            .first()
        )
        if target is None:
            db.rollback()
            raise HTTPException(status_code=404, detail="用户不存在")

        target.password_hash = get_password_hash(body.new_password)
        db.commit()
    except HTTPException:
        raise
    except Exception:
        db.rollback()
        _audit(int(admin_user.id), "reset_password", user_id, "error")
        raise

    fresh = db.query(User).filter(User.id == user_id).first()
    if fresh is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    _audit(int(admin_user.id), "reset_password", user_id, "ok")
    return _user_response(fresh)


# ========== 向量索引运维（ADMIN_TOKEN / localhost）==========


class VectorIndexStatsResponse(BaseModel):
    cached_users: int
    cache_max: int
    dirty_indexes: int
    total_vectors: int
    storage_backend: str


class VectorIndexStatusResponse(BaseModel):
    user_id: int
    in_cache: bool
    is_dirty: bool
    vector_count: int
    deleted_count: int
    last_updated: Optional[str]
    storage_size_kb: Optional[float]


class RebuildResponse(BaseModel):
    success: bool
    message: str


@router.get("/vector-indexes/stats", response_model=VectorIndexStatsResponse)
def get_vector_index_stats(_: bool = Depends(verify_ops_admin)):
    from app.services.vector_search import get_index_stats

    stats = get_index_stats()
    return VectorIndexStatsResponse(**stats)


@router.get("/vector-indexes/{user_id}/status", response_model=VectorIndexStatusResponse)
def get_vector_index_status(
    user_id: int = Path(..., description="用户ID"),
    _: bool = Depends(verify_ops_admin),
):
    from app.services.vector_search import get_user_index_status
    from app.services.vector_storage_local import LocalStorageBackend

    status_data = get_user_index_status(user_id)
    if status_data is None:
        raise HTTPException(status_code=404, detail=f"用户 {user_id} 索引不存在")

    storage_size_kb = None
    try:
        storage = LocalStorageBackend()
        if storage.exists(user_id):
            size_bytes = storage.get_storage_size(user_id)
            storage_size_kb = round(size_bytes / 1024, 2)
    except Exception:
        pass

    return VectorIndexStatusResponse(
        user_id=user_id,
        in_cache=status_data.get("vector_count", 0) > 0,
        is_dirty=status_data.get("is_dirty", False),
        vector_count=status_data.get("vector_count", 0),
        deleted_count=status_data.get("deleted_count", 0),
        last_updated=status_data.get("last_updated"),
        storage_size_kb=storage_size_kb,
    )


@router.post("/vector-indexes/{user_id}/rebuild", response_model=RebuildResponse)
def rebuild_vector_index(
    user_id: int = Path(..., description="用户ID"),
    _: bool = Depends(verify_ops_admin),
    db: Session = Depends(get_db),
):
    from app.services.vector_search import rebuild_user_index_from_db

    try:
        rebuild_user_index_from_db(user_id, db)
        return RebuildResponse(success=True, message=f"用户 {user_id} 的索引重建成功")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"索引重建失败: {str(e)}")


@router.post("/vector-indexes/rebuild-all", response_model=RebuildResponse)
def rebuild_all_vector_indexes(
    _: bool = Depends(verify_ops_admin),
    db: Session = Depends(get_db),
):
    from app.services.vector_search import rebuild_user_index_from_db

    users = db.query(User).all()
    success_count = 0
    error_count = 0
    for user in users:
        try:
            rebuild_user_index_from_db(user.id, db)
            success_count += 1
        except Exception:
            error_count += 1

    return RebuildResponse(
        success=error_count == 0,
        message=f"批量重建完成，成功 {success_count} 个，失败 {error_count} 个",
    )


@router.post("/vector-indexes/persist", response_model=RebuildResponse)
def persist_all_indexes(_: bool = Depends(verify_ops_admin)):
    from app.services.vector_search import persist_all_indexes

    try:
        persist_all_indexes()
        return RebuildResponse(success=True, message="所有脏索引已持久化")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"持久化失败: {str(e)}")
