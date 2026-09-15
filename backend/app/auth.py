"""
认证工具函数
JWT Token 生成/验证、密码哈希/验证、管理员依赖
"""
from datetime import datetime, timedelta
from typing import Optional
from jose import JWTError, jwt
import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from app.config import settings
from app.database import get_db
from app.models import User

# HTTP Bearer Token 安全方案（缺凭证时由依赖显式返回 401）
security = HTTPBearer(auto_error=False)


def _truncate_password_bytes(password: str, max_bytes: int = 72) -> bytes:
    """截断密码到指定字节长度（bcrypt 限制最长 72 字节）。"""
    password_bytes = password.encode("utf-8")
    if len(password_bytes) <= max_bytes:
        return password_bytes
    return password_bytes[:max_bytes]


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """验证密码"""
    password_bytes = _truncate_password_bytes(plain_password, 72)
    try:
        return bcrypt.checkpw(password_bytes, hashed_password.encode("utf-8"))
    except (ValueError, AttributeError):
        if hashed_password.startswith("$2"):
            return bcrypt.checkpw(password_bytes, hashed_password.encode("utf-8"))
        return False


def get_password_hash(password: str) -> str:
    """生成密码哈希"""
    password_bytes = _truncate_password_bytes(password, 72)
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password_bytes, salt)
    return hashed.decode("utf-8")


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """创建 JWT Token"""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(hours=settings.jwt_expiration_hours)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return encoded_jwt


def decode_token(token: str) -> Optional[dict]:
    """解码 JWT Token。失败时不记录 token 原文或前缀。"""
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError:
        return None


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: Session = Depends(get_db),
) -> User:
    """
    获取当前登录用户（依赖注入）。
    未提供/无效 Token → 401；用户不存在或停用 → 401。
    """
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未登录或 Token 缺失",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = decode_token(credentials.credentials)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token 无效或已过期",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id_raw = payload.get("sub")
    if user_id_raw is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token 中缺少用户信息",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        user_id = int(user_id_raw)
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token 中的用户信息格式错误",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = db.query(User).filter(User.id == user_id, User.is_active.is_(True)).first()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户不存在或已被禁用",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user


def get_current_admin_user(current_user: User = Depends(get_current_user)) -> User:
    """要求当前用户为有效管理员。未登录由 get_current_user 返回 401；普通用户 403。"""
    if not bool(getattr(current_user, "is_admin", False)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要管理员权限",
        )
    return current_user


# Alias for readability at call sites
require_admin = get_current_admin_user
