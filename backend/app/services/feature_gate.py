"""
Shared feature gate helpers for partially planned AI modules.
"""
from fastapi import HTTPException, status


def feature_not_enabled(feature_name: str) -> None:
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail={
            "code": "FEATURE_NOT_ENABLED",
            "message": f"{feature_name} 功能尚未启用，当前版本先恢复主程序与核心 AI 检索能力。",
        },
    )
