"""
Unified LLM gateway for GrowthLog AI flows.

Phase C.1.1: DeepSeek is the only runtime provider (OpenAI-compatible API).
MiniMax is no longer an active provider.
P0.1-Fix3: explicit httpx timeouts + typed LLM_PROVIDER_TIMEOUT.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import AsyncIterator, Dict, List, Optional, Tuple

import httpx
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    RateLimitError,
)

from app.config import settings

logger = logging.getLogger(__name__)

DEFAULT_MODEL_KEY = "deepseek-chat"

MODEL_REGISTRY: Dict[str, Dict] = {
    "deepseek-chat": {
        "provider": "deepseek",
        "provider_model": None,  # resolved from settings.deepseek_model at call time
        "display_name": "DeepSeek Chat",
        "default_modes": ["retrieval", "query", "review", "organize"],
        "supports_thinking": False,
        "description": "GrowthLog 默认通用模型（DeepSeek）",
    },
}


class LLMGatewayError(Exception):
    """Raised when the configured LLM provider cannot serve a request."""

    def __init__(self, message: str, code: str = "LLM_GATEWAY_ERROR"):
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass
class LLMGatewayResult:
    content: str
    requested_model_key: str
    used_model_key: str
    fallback_reason: Optional[str] = None


def any_llm_configured() -> bool:
    return bool(settings.deepseek_api_key)


def _provider_configured(provider: str) -> bool:
    if provider == "deepseek":
        return bool(settings.deepseek_api_key)
    return False


def normalize_model_key(model_key: Optional[str]) -> str:
    if not model_key:
        return DEFAULT_MODEL_KEY
    key = model_key.strip().lower()
    aliases = {
        "deepseek": DEFAULT_MODEL_KEY,
        "deepseek-chat": DEFAULT_MODEL_KEY,
        # Legacy client values: remapped to DeepSeek; MiniMax is not a runtime provider.
        "minimax": DEFAULT_MODEL_KEY,
        "minimax-m2.7": DEFAULT_MODEL_KEY,
        "minimax-m2.7-highspeed": DEFAULT_MODEL_KEY,
        "minimax-m27": DEFAULT_MODEL_KEY,
    }
    return aliases.get(key, key)


def ensure_model_provider_ready(model_key: Optional[str] = None) -> str:
    key = normalize_model_key(model_key)
    model = MODEL_REGISTRY.get(key)
    if not model:
        raise LLMGatewayError(f"不支持的模型: {model_key}", code="MODEL_NOT_SUPPORTED")
    if not _provider_configured(model["provider"]):
        raise LLMGatewayError("AI 模型服务未配置", code="LLM_NOT_CONFIGURED")
    return key


def list_available_models(mode: Optional[str] = None) -> List[Dict]:
    items: List[Dict] = []
    normalized_mode = (mode or "").strip().lower()
    for key, meta in MODEL_REGISTRY.items():
        default_modes = list(meta.get("default_modes") or [])
        if normalized_mode and normalized_mode not in default_modes:
            continue
        configured = _provider_configured(meta["provider"])
        items.append({
            "model_key": key,
            "display_name": meta["display_name"],
            "provider": meta["provider"],
            "default_modes": default_modes,
            "supports_thinking": bool(meta.get("supports_thinking", False)),
            "description": meta.get("description", ""),
            "enabled": configured,
            "is_default": key == DEFAULT_MODEL_KEY,
            "provider_configured": configured,
        })
    return items


def default_model_key_for_mode(mode: Optional[str] = None) -> str:
    return DEFAULT_MODEL_KEY


def fallback_candidates_for(model_key: Optional[str], mode: Optional[str] = None) -> List[str]:
    requested = normalize_model_key(model_key) if model_key else default_model_key_for_mode(mode)
    candidates = [requested, DEFAULT_MODEL_KEY]
    deduped: List[str] = []
    for candidate in candidates:
        key = normalize_model_key(candidate)
        if key in MODEL_REGISTRY and key not in deduped:
            deduped.append(key)
    return deduped


def _provider_model_name() -> str:
    name = (settings.deepseek_model or DEFAULT_MODEL_KEY).strip()
    return name or DEFAULT_MODEL_KEY


def build_llm_http_timeout() -> httpx.Timeout:
    """Finite HTTP timeouts for AsyncOpenAI (no silent 600s SDK defaults)."""
    return httpx.Timeout(
        connect=float(settings.llm_connect_timeout_seconds),
        read=float(settings.llm_read_timeout_seconds),
        write=float(settings.llm_write_timeout_seconds),
        pool=float(settings.llm_pool_timeout_seconds),
    )


def llm_max_retries() -> int:
    return max(0, min(int(settings.llm_max_retries), 3))


def _is_timeout_error(exc: BaseException) -> bool:
    if isinstance(exc, (APITimeoutError, httpx.TimeoutException, TimeoutError)):
        return True
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    return "timeout" in name or "timed out" in msg or "timeout" in msg


def _is_rate_limit_error(exc: BaseException) -> bool:
    if isinstance(exc, RateLimitError):
        return True
    status_code = getattr(exc, "status_code", None)
    return status_code == 429


def _is_server_error(exc: BaseException) -> bool:
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int) and status_code >= 500:
        return True
    return isinstance(exc, APIConnectionError)


def _classify_stream_error(exc: BaseException) -> LLMGatewayError:
    """Map provider/timeout/http failures to a stable, user-safe error code."""
    if isinstance(exc, LLMGatewayError):
        return exc
    if _is_timeout_error(exc):
        return LLMGatewayError("AI 模型服务响应超时", code="LLM_PROVIDER_TIMEOUT")
    if _is_rate_limit_error(exc):
        return LLMGatewayError("AI 模型服务请求过于频繁", code="LLM_PROVIDER_RATE_LIMIT")
    if _is_server_error(exc):
        return LLMGatewayError("AI 模型服务暂时不可用", code="LLM_PROVIDER_FAILED")
    if isinstance(exc, APIStatusError):
        return LLMGatewayError("AI 模型服务暂时不可用", code="LLM_PROVIDER_FAILED")
    return LLMGatewayError("AI 模型服务暂时不可用", code="LLM_PROVIDER_FAILED")


def _extract_openai_text(response) -> str:
    try:
        choice = response.choices[0]
        content = getattr(choice.message, "content", None)
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: List[str] = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
                elif hasattr(block, "text"):
                    parts.append(str(block.text))
            return "\n".join(p for p in parts if p).strip()
    except Exception:
        pass
    return ""


async def generate_chat_completion(
    *,
    model_key: Optional[str],
    system: str,
    messages: List[Dict],
    mode: Optional[str] = None,
    max_tokens: int = 4000,
    temperature: float = 0.3,
    user_id: Optional[int] = None,
    fallback_candidates: Optional[List[str]] = None,
    response_format: Optional[Dict] = None,
) -> LLMGatewayResult:
    requested_key = normalize_model_key(model_key) if model_key else default_model_key_for_mode(mode)
    candidates = fallback_candidates or fallback_candidates_for(requested_key, mode)
    last_error: Optional[Exception] = None

    for candidate in candidates:
        key = normalize_model_key(candidate)
        meta = MODEL_REGISTRY.get(key)
        if not meta:
            continue
        if not _provider_configured(meta["provider"]):
            last_error = LLMGatewayError("AI 模型服务未配置", code="LLM_NOT_CONFIGURED")
            continue

        started = time.perf_counter()
        try:
            client = AsyncOpenAI(
                api_key=settings.deepseek_api_key,
                base_url=settings.deepseek_base_url,
                timeout=build_llm_http_timeout(),
                max_retries=llm_max_retries(),
            )
            openai_messages: List[Dict] = []
            if system:
                openai_messages.append({"role": "system", "content": system})
            for msg in messages or []:
                role = msg.get("role") if isinstance(msg, dict) else None
                content = msg.get("content") if isinstance(msg, dict) else None
                if role in ("user", "assistant", "system") and content is not None:
                    openai_messages.append({"role": role, "content": str(content)})

            create_kwargs: Dict = {
                "model": _provider_model_name(),
                "messages": openai_messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
            if response_format:
                create_kwargs["response_format"] = response_format
            response = await client.chat.completions.create(**create_kwargs)
            content = _extract_openai_text(response)
            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            logger.info(
                "LLM request ok: provider=%s model_key=%s user_id=%s json_mode=%s elapsed_ms=%s",
                meta["provider"],
                key,
                user_id,
                bool(response_format),
                elapsed_ms,
            )
            return LLMGatewayResult(
                content=content,
                requested_model_key=requested_key,
                used_model_key=DEFAULT_MODEL_KEY,
                fallback_reason=None,
            )
        except Exception as exc:
            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            err_type = type(exc).__name__
            # Never log query/body/keys — type + elapsed only.
            logger.warning(
                "LLM request failed: provider=%s model_key=%s user_id=%s error=%s elapsed_ms=%s",
                meta["provider"],
                key,
                user_id,
                err_type,
                elapsed_ms,
            )
            if _is_timeout_error(exc):
                raise LLMGatewayError(
                    "AI 模型服务响应超时",
                    code="LLM_PROVIDER_TIMEOUT",
                ) from exc
            last_error = exc

    if isinstance(last_error, LLMGatewayError):
        raise last_error
    raise LLMGatewayError("AI 模型服务暂时不可用", code="LLM_PROVIDER_FAILED")


async def stream_chat_completion(
    *,
    model_key: Optional[str],
    system: str,
    messages: List[Dict],
    mode: Optional[str] = None,
    max_tokens: int = 4000,
    temperature: float = 0.3,
    user_id: Optional[int] = None,
    fallback_candidates: Optional[List[str]] = None,
) -> AsyncIterator[Tuple[str, Optional["LLMGatewayResult"]]]:
    """
    Real provider streaming (R11.3 / R11.3-Fix).

    Yields ("delta", text) for every content chunk, then exactly one
    ("done", LLMGatewayResult) when the provider stream completes normally.

    Fallback across `fallback_candidates` is only attempted while no delta has
    been yielded yet. Once at least one delta has reached the caller, a
    provider failure raises LLMGatewayError immediately — never splices
    another model's output into an in-flight stream.

    asyncio.CancelledError always propagates unchanged: no fallback, no further
    provider consumption, no error remapping. Provider stream + AsyncOpenAI
    client are always closed in finally.
    """
    import asyncio

    requested_key = normalize_model_key(model_key) if model_key else default_model_key_for_mode(mode)
    candidates = fallback_candidates or fallback_candidates_for(requested_key, mode)
    last_error: Optional[Exception] = None
    started_delta = False

    for candidate in candidates:
        key = normalize_model_key(candidate)
        meta = MODEL_REGISTRY.get(key)
        if not meta:
            continue
        if not _provider_configured(meta["provider"]):
            last_error = LLMGatewayError("AI 模型服务未配置", code="LLM_NOT_CONFIGURED")
            continue

        started = time.perf_counter()
        content_parts: List[str] = []
        client = None
        stream = None
        try:
            client = AsyncOpenAI(
                api_key=settings.deepseek_api_key,
                base_url=settings.deepseek_base_url,
                timeout=build_llm_http_timeout(),
                max_retries=llm_max_retries(),
            )
            openai_messages: List[Dict] = []
            if system:
                openai_messages.append({"role": "system", "content": system})
            for msg in messages or []:
                role = msg.get("role") if isinstance(msg, dict) else None
                content = msg.get("content") if isinstance(msg, dict) else None
                if role in ("user", "assistant", "system") and content is not None:
                    openai_messages.append({"role": role, "content": str(content)})

            stream = await client.chat.completions.create(
                model=_provider_model_name(),
                messages=openai_messages,
                max_tokens=max_tokens,
                temperature=temperature,
                stream=True,
            )
            async for chunk in stream:
                try:
                    choice = chunk.choices[0]
                except (IndexError, AttributeError, TypeError):
                    continue
                delta = getattr(choice, "delta", None)
                text = getattr(delta, "content", None) if delta is not None else None
                if not text:
                    continue
                started_delta = True
                content_parts.append(text)
                yield ("delta", text)

            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            logger.info(
                "LLM stream ok: provider=%s model_key=%s user_id=%s elapsed_ms=%s",
                meta["provider"],
                key,
                user_id,
                elapsed_ms,
            )
            result = LLMGatewayResult(
                content="".join(content_parts).strip(),
                requested_model_key=requested_key,
                used_model_key=DEFAULT_MODEL_KEY,
                fallback_reason=(
                    f"primary_model_unavailable:{requested_key}->{key}"
                    if key != requested_key
                    else None
                ),
            )
            yield ("done", result)
            return
        except asyncio.CancelledError:
            # Must propagate; never fallback / continue consuming / emit error.
            raise
        except Exception as exc:
            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            err_type = type(exc).__name__
            logger.warning(
                "LLM stream failed: provider=%s model_key=%s user_id=%s error=%s "
                "elapsed_ms=%s started_delta=%s",
                meta["provider"],
                key,
                user_id,
                err_type,
                elapsed_ms,
                started_delta,
            )
            typed_error = _classify_stream_error(exc)
            if started_delta:
                raise typed_error from exc
            last_error = typed_error
            continue
        finally:
            # Close provider stream exactly once: prefer aclose, else close.
            if stream is not None:
                try:
                    aclose = getattr(stream, "aclose", None)
                    if callable(aclose):
                        await aclose()
                    else:
                        close = getattr(stream, "close", None)
                        if callable(close):
                            result = close()
                            if asyncio.iscoroutine(result):
                                await result
                except Exception:
                    pass
            if client is not None:
                try:
                    await client.close()
                except Exception:
                    pass

    if isinstance(last_error, LLMGatewayError):
        raise last_error
    raise LLMGatewayError("AI 模型服务暂时不可用", code="LLM_PROVIDER_FAILED")
