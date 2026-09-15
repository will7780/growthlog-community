"""
认证路由
POST /api/auth/login - 登录
GET /api/auth/me - 获取当前用户信息
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import User
from app.schemas import LoginRequest, LoginResponse, UserResponse, ErrorResponse, ErrorDetail
from app.auth import verify_password, create_access_token, get_current_user

router = APIRouter()


@router.post("/login", response_model=LoginResponse)
def login(
    request: LoginRequest,
    db: Session = Depends(get_db)
):
    """
    用户登录
    验证用户名密码，返回 JWT Token
    """
    # 查询用户
    user = db.query(User).filter(User.username == request.username).first()
    
    # 验证用户和密码
    if user is None or not verify_password(request.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误"
        )
    
    # 检查用户是否激活
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户已被禁用"
        )
    
    # 生成 Token（sub 字段存储 user_id，JWT 标准要求 sub 必须是字符串）
    access_token = create_access_token(data={"sub": str(user.id)})
    
    return LoginResponse(
        access_token=access_token,
        token_type="bearer",
        user=UserResponse(
            id=user.id,
            username=user.username,
            is_active=user.is_active,
            is_admin=bool(getattr(user, "is_admin", False)),
            can_edit_delete_own_entries=bool(
                getattr(user, "can_edit_delete_own_entries", False)
            ),
            created_at=user.created_at,
        ),
    )


@router.get("/me", response_model=UserResponse)
def get_me(current_user: User = Depends(get_current_user)):
    """
    获取当前登录用户信息
    """
    return UserResponse(
        id=current_user.id,
        username=current_user.username,
        is_active=current_user.is_active,
        is_admin=bool(getattr(current_user, "is_admin", False)),
        can_edit_delete_own_entries=bool(
            getattr(current_user, "can_edit_delete_own_entries", False)
        ),
        created_at=current_user.created_at,
    )
