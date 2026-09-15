"""
Shared structured JSON LLM calls for Judge, rubric, and organizer (R11.6).

Prefer provider JSON response_format when available; always validate with
Pydantic or a caller-supplied validator. One temperature=0 repair max.
Never log query bodies, reference keys, model text, or secrets.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Type, TypeVar

from pydantic import BaseModel, ValidationError

from app.services.llm_gateway import LLMGatewayError, generate_chat_completion

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.I)


class StructuredLLMError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code
        self.message = message or code


@dataclass
class StructuredJsonResult:
    data: Dict[str, Any]
    model: Optional[BaseModel]
    repaired: bool
    used_json_mode: bool
    elapsed_ms: float


def extract_json_text(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        raise StructuredLLMError("JSON_SYNTAX", "empty")
    try:
        json.loads(text)
        return text
    except json.JSONDecodeError:
        pass
    m = JSON_FENCE_RE.search(text)
    if m:
        candidate = m.group(1).strip()
        json.loads(candidate)
        return candidate
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidate = text[start : end + 1]
        json.loads(candidate)
        return candidate
    raise StructuredLLMError("JSON_SYNTAX", "unparseable")


def _safe_stage_log(
    *,
    stage: str,
    error_code: Optional[str],
    request_id: Optional[str],
    candidate_count: Optional[int],
    batch_count: Optional[int],
    elapsed_ms: float,
) -> None:
    logger.info(
        "structured_llm stage=%s request_id=%s error_code=%s candidate_count=%s "
        "batch_count=%s elapsed_ms=%s",
        stage,
        request_id or "-",
        error_code or "ok",
        candidate_count if candidate_count is not None else "-",
        batch_count if batch_count is not None else "-",
        round(elapsed_ms, 1),
    )


async def generate_structured_json(
    *,
    system: str,
    messages: List[Dict[str, str]],
    mode: Optional[str] = None,
    model_key: Optional[str] = None,
    user_id: Optional[int] = None,
    fallback_candidates: Optional[List[str]] = None,
    max_tokens: int = 3000,
    temperature: float = 0.0,
    schema_model: Optional[Type[T]] = None,
    validate_fn: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
    allow_repair: bool = True,
    prefer_json_mode: bool = True,
    request_id: Optional[str] = None,
    stage: str = "structured",
    candidate_count: Optional[int] = None,
    batch_count: Optional[int] = None,
    syntax_error_code: str = "JSON_SYNTAX",
    schema_error_code: str = "SCHEMA_INVALID",
    provider_error_code: str = "ORGANIZE_PROVIDER_FAILED",
) -> StructuredJsonResult:
    """
    Call LLM for a JSON object. When prefer_json_mode, pass response_format.
    On syntax/schema failure, optionally one repair at temperature=0.
    """
    started = time.perf_counter()
    used_json_mode = False

    async def _once(temp: float, repair_note: Optional[str] = None) -> str:
        nonlocal used_json_mode
        msgs = list(messages)
        if repair_note:
            msgs = msgs + [{"role": "user", "content": repair_note}]
        kwargs: Dict[str, Any] = {
            "model_key": model_key,
            "system": system,
            "messages": msgs,
            "mode": mode,
            "max_tokens": max_tokens,
            "temperature": temp,
            "user_id": user_id,
            "fallback_candidates": fallback_candidates,
        }
        if prefer_json_mode:
            kwargs["response_format"] = {"type": "json_object"}
            used_json_mode = True
        try:
            result = await generate_chat_completion(**kwargs)
            return result.content or ""
        except LLMGatewayError as exc:
            raise StructuredLLMError(provider_error_code, exc.code) from exc
        except TypeError as exc:
            # Provider/runtime TypeError is not signature compatibility; fail once.
            raise StructuredLLMError(provider_error_code, "type_error") from exc

    def _validate(raw: str) -> StructuredJsonResult:
        try:
            text = extract_json_text(raw)
            payload = json.loads(text)
        except StructuredLLMError:
            raise
        except json.JSONDecodeError as exc:
            raise StructuredLLMError(syntax_error_code, "decode") from exc
        if not isinstance(payload, dict):
            raise StructuredLLMError(schema_error_code, "root_not_object")
        model_obj: Optional[BaseModel] = None
        data = payload
        if schema_model is not None:
            try:
                model_obj = schema_model.model_validate(payload)
                data = model_obj.model_dump()
            except ValidationError as exc:
                raise StructuredLLMError(schema_error_code, "pydantic") from exc
        if validate_fn is not None:
            try:
                data = validate_fn(data)
            except StructuredLLMError:
                raise
            except Exception as exc:
                raise StructuredLLMError(schema_error_code, type(exc).__name__) from exc
        return StructuredJsonResult(
            data=data,
            model=model_obj,
            repaired=False,
            used_json_mode=used_json_mode,
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )

    try:
        raw = await _once(temperature)
        out = _validate(raw)
        _safe_stage_log(
            stage=stage,
            error_code=None,
            request_id=request_id,
            candidate_count=candidate_count,
            batch_count=batch_count,
            elapsed_ms=out.elapsed_ms,
        )
        return out
    except StructuredLLMError as first_err:
        if first_err.code == provider_error_code or not allow_repair:
            _safe_stage_log(
                stage=stage,
                error_code=first_err.code,
                request_id=request_id,
                candidate_count=candidate_count,
                batch_count=batch_count,
                elapsed_ms=(time.perf_counter() - started) * 1000,
            )
            raise
        try:
            raw2 = await _once(
                0.0,
                repair_note=(
                    "上一次输出不是合法且完整的目标 JSON。"
                    "请仅输出一个符合 schema 的 JSON 对象，不要 markdown，不要额外说明。"
                ),
            )
            out = _validate(raw2)
            out.repaired = True
            out.elapsed_ms = (time.perf_counter() - started) * 1000
            _safe_stage_log(
                stage=stage,
                error_code=None,
                request_id=request_id,
                candidate_count=candidate_count,
                batch_count=batch_count,
                elapsed_ms=out.elapsed_ms,
            )
            return out
        except StructuredLLMError as second_err:
            _safe_stage_log(
                stage=stage,
                error_code=second_err.code,
                request_id=request_id,
                candidate_count=candidate_count,
                batch_count=batch_count,
                elapsed_ms=(time.perf_counter() - started) * 1000,
            )
            raise
