"""
Model selection helpers.

Phase 0 keeps model permissions config-light: all authenticated users may use
configured built-in models. A later phase can replace this module with database
backed per-user permissions without changing route contracts.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.auth import User
from app.services.llm_gateway import (
    LLMGatewayError,
    default_model_key_for_mode,
    ensure_model_provider_ready,
    fallback_candidates_for,
    list_available_models,
    normalize_model_key,
)


def list_ai_models_for_user(db: Session, user: User, mode: Optional[str] = None) -> Dict:
    models = list_available_models(mode)
    recommended = default_model_key_for_mode(mode)
    if not any(item["model_key"] == recommended for item in models):
        recommended = models[0]["model_key"] if models else default_model_key_for_mode(None)
    return {
        "models": models,
        "recommended_default": recommended,
        "mode": mode,
    }


def resolve_model_key_for_mode(
    db: Session,
    user: User,
    mode: Optional[str],
    model_key: Optional[str],
) -> str:
    resolved = normalize_model_key(model_key) if model_key else default_model_key_for_mode(mode)
    try:
        return ensure_model_provider_ready(resolved)
    except LLMGatewayError as exc:
        code = exc.code or "LLM_GATEWAY_ERROR"
        if code == "MODEL_NOT_SUPPORTED":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "MODEL_NOT_SUPPORTED",
                    "message": "不支持的模型，请选择可用模型",
                },
            ) from exc
        if code == "LLM_NOT_CONFIGURED":
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "LLM_NOT_CONFIGURED",
                    "message": "AI 模型服务未配置，请联系管理员",
                },
            ) from exc
        # LLM_PROVIDER_FAILED and other typed gateway failures
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "LLM_PROVIDER_FAILED" if code == "LLM_PROVIDER_FAILED" else code,
                "message": "AI 服务暂时不可用，请稍后重试",
            },
        ) from exc
    # Unexpected exceptions must not be disguised as "model not configured".


def get_fallback_candidates(db: Session, user: User, requested_model_key: str) -> List[str]:
    return fallback_candidates_for(requested_model_key)
