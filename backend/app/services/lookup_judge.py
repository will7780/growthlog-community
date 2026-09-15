"""
R5.5E / R5.5D generic LLM-only relevance judgment.

LLM owns found / matches / relevant. Server owns candidate completeness,
batch coverage, dedupe, isolation, scheduling, and failure semantics.

Strategy C (default): shared relevance_rubric (canonical semantics) +
Pass A full coverage + Pass B full coverage (deterministic reshuffle) +
third-round on A/B disagreement.

lookup_all uses recall-first union (A∨B; third may add, cannot veto positives).
lookup_one / answer keep third-wins precision semantics.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from app.eval.r5_schema import (
    E_JUDGE_COVERAGE,
    E_JUDGE_DUPLICATE_KEY,
    E_JUDGE_FOUND_INCONSISTENT,
    E_JUDGE_FOUND_INVALID,
    E_JUDGE_INVALID_JSON,
    E_JUDGE_MISSING_KEY,
    E_JUDGE_PROVIDER_FAILED,
    E_JUDGE_SCHEMA,
    E_JUDGE_UNKNOWN_KEY,
    INTENT_ANSWER,
    INTENT_LOOKUP_ALL,
    INTENT_LOOKUP_ONE,
    JUDGE_TEMPERATURE,
    LOOKUP_INTENTS,
    JudgeValidationError,
    apply_validated_judge,
    judge_failure_from_exception,
    validate_intent,
)
from app.services.llm_gateway import LLMGatewayError, generate_chat_completion
from app.services.query_normalize import normalize_query, primary_topic_terms
from app.services.reference_identity import annotate_with_canonical_keys

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 8  # R5.5E: smaller batches reduce LLM key-omission rate
RUBRIC_VERSION = "r5.5e.1"

LOOKUP_JUDGE_SYSTEM = """你是通用检索相关性裁决器，适用于任意主题查询（政治、项目、课堂笔记、技术、Prompt 等）。
只能使用服务端提供的 reference_key。
必须输出严格 JSON（不要 markdown）：
{
  "intent": "lookup_one|lookup_all|answer",
  "found": true|false,
  "judgments": [
    {"reference_key":"...", "relevant": true|false, "reason_code":"direct_topic|semantic_equivalent|incidental|unrelated"}
  ]
}
裁决原则（通用，禁止套用固定业务主题模板）：
- 必须遵守本请求提供的 relevance_rubric（inclusion / incidental exclusion）。
- relevant=true：记录中存在与用户查询相关的实质性可用内容即可（direct_topic 或 semantic_equivalent），即使整条记录还有更大/其他主题。
- 不得要求标题或整条记录的“唯一主主题”必须与查询完全一致。
- incidental：仅孤立词语/顺带提及，且没有可复用的相关内容 → relevant=false。
- unrelated：无实质相关内容 → relevant=false。
- 服务端提供的主题词、同义词、标题、命中窗口及 title_term_hit 等只是证据，不得替代你的独立判断。
- found 必须是 JSON 布尔；本批 found=true 当且仅当本批至少一条 relevant=true。
- lookup_one 与 lookup_all：必须对本批每一个允许 key 给出且仅一条 judgment。
- 不得把服务端失败伪装成“没有找到”。
"""

RUBRIC_SYSTEM = """你是检索相关性量规编写器。根据 canonical_lookup_semantics 与原始问法写一份通用、可复用的 relevance_rubric。
禁止读取或编造业务 ID、case_id、gold 列表。
必须输出严格 JSON（不要 markdown）：
{
  "target_topic": "短主题描述",
  "inclusion_criteria": ["实质性相关即可入选的判据", "..."],
  "incidental_exclusion_criteria": ["仅孤立词/顺带提及应排除的判据", "..."],
  "rubric_version": "r5.5e.1"
}
要求：
- 必须以 canonical topic_terms / synonyms 界定主题边界；等价问法（中英文、空格、列出全部等）必须得到相同纳入边界。
- 量规必须主题无关可迁移，不得写死某一产品/作业/Prompt 特判。
- inclusion：存在可用相关内容即可，不要求整条记录主主题完全一致。
- exclusion：仅偶然提及、无可用内容时排除。
"""

# Canonical evidence fields for digest + judge serialization (fixed order).
_EVIDENCE_DIGEST_FIELDS: Tuple[str, ...] = (
    "title",
    "hit_window",
    "window_sources",
    "window_count",
    "title_term_hit",
    "body_term_hit",
    "title_term_hit_count",
    "body_term_hit_count",
    "term_hit_count",
    "content_char_len",
    "short_note",
    "topic_term_absent",
    "evidence_mode",
    "evidence_coverage",
    "truncated",
    "source_type",
    "entry_id",
    "attachment_id",
    "chunk_id",
    "modality",
    "retrieval_method",
)


def _judge_strategy() -> str:
    raw = (os.getenv("RAG_LOOKUP_JUDGE_STRATEGY") or "C").strip().upper()
    return raw if raw in {"A", "B", "C"} else "C"


def detect_lookup_intent(query: str, *, hint: Optional[str] = None) -> str:
    if hint:
        try:
            return validate_intent(hint)
        except JudgeValidationError:
            pass
    q = (query or "").strip().lower()
    exhaustive_markers = (
        "所有",
        "全部",
        "列出",
        "罗列",
        "有哪些",
        "list all",
        "show all",
        "every",
    )
    one_markers = ("找一个", "找一下", "帮我找", "哪一条", "哪篇", "find one")
    if any(m in q for m in exhaustive_markers):
        return INTENT_LOOKUP_ALL
    if any(m in q for m in one_markers):
        return INTENT_LOOKUP_ONE
    return INTENT_ANSWER


def _batch_size() -> int:
    raw = (os.getenv("RAG_LOOKUP_JUDGE_BATCH_SIZE") or "").strip()
    if raw.isdigit():
        return max(5, min(80, int(raw)))
    return DEFAULT_BATCH_SIZE


def _chunk_keys(keys: Sequence[str], size: int) -> List[List[str]]:
    if size <= 0:
        size = DEFAULT_BATCH_SIZE
    out: List[List[str]] = []
    buf: List[str] = []
    for key in keys:
        buf.append(key)
        if len(buf) >= size:
            out.append(buf)
            buf = []
    if buf:
        out.append(buf)
    return out


def build_canonical_lookup_semantics(
    query: str,
    *,
    intent: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Stable lookup semantics for rubric generation.

    Equivalence-preserving across CJK/Latin spacing and list/find operators.
    Never reads gold / case_id / entry_id.
    """
    intent_n = detect_lookup_intent(query, hint=intent)
    nq = normalize_query(query)
    terms = primary_topic_terms(nq) or list(nq.expanded_terms)
    # Sorted unique content terms (operators already stripped by primary_topic_terms).
    topic_terms = sorted({str(t).strip().lower() for t in terms if str(t).strip()})
    synonyms = sorted({str(h).strip().lower() for h in (nq.synonym_hits or []) if str(h).strip()})
    scope = {
        INTENT_LOOKUP_ALL: "exhaustive_list",
        INTENT_LOOKUP_ONE: "single_best",
        INTENT_ANSWER: "answer_context",
    }.get(intent_n, "answer_context")
    # Compact canonical string — topic boundary only (no raw query required for equality).
    canonical_topic = "|".join(topic_terms)
    return {
        "intent": intent_n,
        "scope": scope,
        "topic_terms": topic_terms,
        "synonym_links": synonyms,
        "canonical_topic": canonical_topic,
        "rubric_version": RUBRIC_VERSION,
    }


def canonical_evidence_payload(evidence: Mapping[str, Any]) -> Dict[str, Any]:
    """Fixed-field evidence payload for digest / serialization stability."""
    payload: Dict[str, Any] = {}
    for key in _EVIDENCE_DIGEST_FIELDS:
        val = evidence.get(key)
        if key == "window_sources":
            payload[key] = list(val) if isinstance(val, (list, tuple)) else []
        elif key in {
            "title_term_hit",
            "body_term_hit",
            "short_note",
            "topic_term_absent",
            "truncated",
        }:
            payload[key] = bool(val)
        elif key in {
            "title_term_hit_count",
            "body_term_hit_count",
            "term_hit_count",
            "content_char_len",
            "window_count",
        }:
            payload[key] = int(val or 0)
        elif key == "evidence_coverage":
            try:
                payload[key] = float(val if val is not None else 0.0)
            except Exception:
                payload[key] = 0.0
        elif key in {"title", "hit_window", "evidence_mode"}:
            payload[key] = str(val or "")
        else:
            payload[key] = val if val is not None else None
    return payload


def compute_evidence_digest(evidence: Mapping[str, Any]) -> str:
    payload = canonical_evidence_payload(evidence)
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _snippet_for_ref(ref: Mapping[str, Any], max_len: int = 1600) -> str:
    """Prefer bounded evidence windows; fixed field order for stability."""
    evidence = ref.get("judge_evidence") if isinstance(ref.get("judge_evidence"), dict) else {}
    canon = canonical_evidence_payload(evidence)
    title = canon["title"] or str(ref.get("title") or "")
    window = canon["hit_window"] or str(ref.get("snippet") or "")
    if not window:
        window = str(ref.get("content") or "")[:max_len]
    meta_bits = []
    digest = evidence.get("evidence_digest") or compute_evidence_digest(evidence)
    meta_bits.append(f"evidence_digest={digest}")
    for key in (
        "source_type",
        "entry_id",
        "attachment_id",
        "chunk_id",
        "modality",
        "retrieval_method",
        "evidence_mode",
    ):
        val = canon.get(key, ref.get(key))
        if val is not None and val != "":
            meta_bits.append(f"{key}={val}")
    for flag in ("title_term_hit", "body_term_hit", "topic_term_absent", "short_note"):
        if canon.get(flag):
            meta_bits.append(f"{flag}=true")
    for count_key in ("title_term_hit_count", "body_term_hit_count", "term_hit_count", "content_char_len"):
        val = canon.get(count_key)
        if isinstance(val, int) and val > 0:
            meta_bits.append(f"{count_key}={val}")
    sources = canon.get("window_sources") or []
    if sources:
        meta_bits.append(f"window_sources={','.join(str(s) for s in sources)}")
    head = f"标题: {title}\n命中窗口: {window}".strip()
    if meta_bits:
        head = f"{' '.join(meta_bits)}\n{head}"
    return head[: max_len + 240]


def _assert_found_judgment_consistency(
    *,
    found: bool,
    judgments: Sequence[Mapping[str, Any]],
) -> None:
    any_relevant = any(bool(j.get("relevant")) for j in judgments)
    if found is False and any_relevant:
        raise JudgeValidationError(
            "found=false but judgments contain relevant=true",
            code=E_JUDGE_FOUND_INCONSISTENT,
        )
    if found is True and not any_relevant:
        raise JudgeValidationError(
            "found=true but no relevant=true in judgments",
            code=E_JUDGE_FOUND_INCONSISTENT,
        )


def _format_rubric(rubric: Mapping[str, Any]) -> str:
    return json.dumps(
        {
            "target_topic": str(rubric.get("target_topic") or ""),
            "inclusion_criteria": list(rubric.get("inclusion_criteria") or []),
            "incidental_exclusion_criteria": list(
                rubric.get("incidental_exclusion_criteria") or []
            ),
            "rubric_version": str(rubric.get("rubric_version") or RUBRIC_VERSION),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _validate_relevance_rubric(payload: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise JudgeValidationError("rubric must be object", code=E_JUDGE_SCHEMA)
    topic = str(payload.get("target_topic") or "").strip()
    inclusion = payload.get("inclusion_criteria")
    exclusion = payload.get("incidental_exclusion_criteria")
    if not topic:
        raise JudgeValidationError("rubric missing target_topic", code=E_JUDGE_SCHEMA)
    if not isinstance(inclusion, list) or not inclusion:
        raise JudgeValidationError("rubric missing inclusion_criteria", code=E_JUDGE_SCHEMA)
    if not isinstance(exclusion, list) or not exclusion:
        raise JudgeValidationError(
            "rubric missing incidental_exclusion_criteria",
            code=E_JUDGE_SCHEMA,
        )
    blob = json.dumps(payload, ensure_ascii=False).lower()
    banned_markers = (
        "case_id",
        "entry_id",
        "gold20",
        "attachment_id",
        "prompt" + "_gold",
    )
    for banned in banned_markers:
        if banned in blob:
            raise JudgeValidationError(
                "rubric must not include business identifiers",
                code=E_JUDGE_SCHEMA,
            )
    return {
        "target_topic": topic,
        "inclusion_criteria": [str(x) for x in inclusion],
        "incidental_exclusion_criteria": [str(x) for x in exclusion],
        "rubric_version": str(payload.get("rubric_version") or RUBRIC_VERSION),
    }


def _pass_b_key_order(keys: Sequence[str]) -> List[str]:
    """Deterministic reshuffle different from Pass A order (reverse + rotate)."""
    if not keys:
        return []
    rev = list(reversed(list(keys)))
    digest = hashlib.sha256("|".join(rev).encode("utf-8")).hexdigest()
    rot = int(digest[:8], 16) % len(rev)
    return rev[rot:] + rev[:rot]


async def _generate_relevance_rubric(
    *,
    query: str,
    model_key: Optional[str],
    user_id: Optional[int],
    fallback_candidates: Optional[List[str]],
    intent: Optional[str] = None,
    canonical_semantics: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """One LLM rubric per lookup request. Must not receive gold/case/business IDs."""
    semantics = dict(canonical_semantics or build_canonical_lookup_semantics(query, intent=intent))
    user_prompt = (
        f"canonical_lookup_semantics: {json.dumps(semantics, ensure_ascii=False, sort_keys=True)}\n"
        f"原始用户问题: {query}\n"
        "请基于 canonical topic_terms/synonyms 输出通用 relevance_rubric JSON；"
        "等价问法必须保持相同纳入边界。"
        "不得包含 case_id、entry_id、gold 或任何业务主键。"
    )
    try:
        llm = await generate_chat_completion(
            model_key=model_key,
            system=RUBRIC_SYSTEM,
            messages=[{"role": "user", "content": user_prompt}],
            mode="retrieval",
            max_tokens=800,
            temperature=JUDGE_TEMPERATURE,
            user_id=user_id,
            fallback_candidates=fallback_candidates,
        )
    except LLMGatewayError as exc:
        raise JudgeValidationError(
            f"rubric provider failed: {exc}",
            code=E_JUDGE_PROVIDER_FAILED,
        ) from exc
    raw = (llm.content or "").strip()
    try:
        payload = json.loads(raw)
    except Exception as exc:
        raise JudgeValidationError(
            f"invalid rubric JSON: {type(exc).__name__}",
            code=E_JUDGE_INVALID_JSON,
        ) from exc
    return _validate_relevance_rubric(payload)


def _build_batch_user_prompt(
    *,
    query: str,
    intent: str,
    annotated: Sequence[Mapping[str, Any]],
    allowed: Sequence[str],
    rubric: Optional[Mapping[str, Any]],
    repair_note: str = "",
) -> str:
    parts = []
    for ref in annotated:
        parts.append(
            f"【reference_key={ref['reference_key']}】\n{_snippet_for_ref(ref)}"
        )
    nq = normalize_query(query)
    topic_terms = primary_topic_terms(nq) or list(nq.expanded_terms)
    synonym_hits = list(nq.synonym_hits or [])
    coverage_note = (
        "对本批每一个允许 key 必须给出且仅给出一条 judgment（lookup_one 与 lookup_all 相同）。"
    )
    found_note = (
        "本批 found=true 当且仅当本批至少一条 relevant=true；"
        "本批若全部 relevant=false 则 found 必须为 false。"
        "有实质性相关内容即 relevant，即使记录还有更大主题；仅孤立词/顺带提及才 incidental。"
    )
    rubric_block = f"relevance_rubric: {_format_rubric(rubric)}\n" if rubric else ""
    repair = f"{repair_note}\n" if repair_note else ""
    return (
        f"intent={intent}\n"
        f"用户问题: {query}\n"
        f"{rubric_block}"
        f"规范化主题词（证据）: {json.dumps(topic_terms, ensure_ascii=False)}\n"
        f"同义词命中（证据）: {json.dumps(synonym_hits, ensure_ascii=False)}\n"
        f"说明: 主题词/同义词/命中窗口仅为证据；请按 rubric 与当前问题做通用相关性判断，"
        f"不要套用固定业务主题模板。\n"
        f"{repair}"
        f"{coverage_note}\n"
        f"{found_note}\n"
        f"允许的 reference_key: {json.dumps(list(allowed), ensure_ascii=False)}\n\n"
        f"候选证据（固定字段顺序）:\n" + "\n\n".join(parts) + "\n\n"
        "请输出严格 JSON。"
    )


# Protocol failures eligible for one full-batch repair + optional targeted 补审.
_REPAIRABLE_JUDGE_CODES = frozenset(
    {
        E_JUDGE_MISSING_KEY,
        E_JUDGE_INVALID_JSON,
        E_JUDGE_SCHEMA,
        E_JUDGE_DUPLICATE_KEY,
        E_JUDGE_UNKNOWN_KEY,
        E_JUDGE_FOUND_INVALID,
        E_JUDGE_FOUND_INCONSISTENT,
        E_JUDGE_COVERAGE,
    }
)


async def _llm_judge_raw(
    *,
    user_prompt: str,
    model_key: Optional[str],
    user_id: Optional[int],
    fallback_candidates: Optional[List[str]],
    prefer_json_mode: bool = True,
    max_tokens: int = 3000,
    temperature: float = JUDGE_TEMPERATURE,
) -> str:
    """Raw judge call; prefers JSON response_format when provider supports it."""
    try:
        kwargs: Dict[str, Any] = {
            "model_key": model_key,
            "system": LOOKUP_JUDGE_SYSTEM,
            "messages": [{"role": "user", "content": user_prompt}],
            "mode": "retrieval",
            "max_tokens": max_tokens,
            "temperature": temperature,
            "user_id": user_id,
            "fallback_candidates": fallback_candidates,
        }
        if prefer_json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        llm = await generate_chat_completion(**kwargs)
    except LLMGatewayError as exc:
        raise JudgeValidationError(
            f"provider failed: {type(exc).__name__}",
            code=E_JUDGE_PROVIDER_FAILED,
        ) from exc
    except TypeError as exc:
        # Provider/runtime TypeError is not a signature-compat signal; fail once.
        raise JudgeValidationError(
            "provider type error",
            code=E_JUDGE_PROVIDER_FAILED,
        ) from exc
    return llm.content or ""


def _validate_batch_payload(
    *,
    intent: str,
    allowed: Sequence[str],
    raw_payload: str,
) -> Dict[str, Any]:
    from app.services.structured_llm import extract_json_text

    try:
        normalized = extract_json_text(raw_payload)
    except Exception as exc:
        raise JudgeValidationError(
            f"invalid judge JSON: {type(exc).__name__}",
            code=E_JUDGE_INVALID_JSON,
        ) from exc
    try:
        validated = apply_validated_judge(
            intent=intent,
            candidate_keys=allowed,
            raw_payload=normalized,
        )
    except JudgeValidationError:
        raise
    except Exception as exc:
        raise JudgeValidationError(
            f"invalid judge JSON: {type(exc).__name__}",
            code=E_JUDGE_INVALID_JSON,
        ) from exc
    if "found" not in validated or not isinstance(validated.get("found"), bool):
        raise JudgeValidationError(
            "lookup requires explicit boolean found",
            code=E_JUDGE_FOUND_INVALID,
        )
    judgments = validated.get("judgments") or []
    if not isinstance(judgments, list):
        raise JudgeValidationError("judgments must be a list", code=E_JUDGE_SCHEMA)
    _assert_found_judgment_consistency(found=bool(validated["found"]), judgments=judgments)
    return validated


def _partial_judgments_from_raw(
    raw: str, allowed: Sequence[str]
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Best-effort parse of LLM rows for merge; never invents relevant=true."""
    from app.eval.r5_schema import parse_judge_payload
    from app.services.structured_llm import extract_json_text

    allowed_set = set(allowed)
    try:
        text = extract_json_text(raw)
        partial = parse_judge_payload(text)
    except Exception:
        return [], list(allowed)
    rows: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for j in partial.get("judgments") or []:
        key = str(j.get("reference_key") or "").strip()
        if not key or key not in allowed_set or key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "reference_key": key,
                "relevant": bool(j.get("relevant")),
                "reason_code": str(j.get("reason_code") or ""),
            }
        )
    missing = [k for k in allowed if k not in seen]
    return rows, missing


async def _recover_batch_individually(
    *,
    query: str,
    intent: str,
    allowed: Sequence[str],
    by_key: Mapping[str, Mapping[str, Any]],
    model_key: Optional[str],
    user_id: Optional[int],
    fallback_candidates: Optional[List[str]],
    rubric: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Recover an unusable batch with strict, LLM-only single-item judgments."""
    rows: List[Dict[str, Any]] = []
    for key in allowed:
        ref = by_key.get(key)
        if ref is None:
            raise JudgeValidationError(
                "single recovery reference missing",
                code=E_JUDGE_UNKNOWN_KEY,
            )
        rows.append(
            await _llm_single_candidate_adjudicate(
                query=query,
                intent=intent,
                ref=ref,
                model_key=model_key,
                user_id=user_id,
                fallback_candidates=fallback_candidates,
                rubric=rubric,
            )
        )
    return _validate_batch_payload(
        intent=intent,
        allowed=allowed,
        raw_payload=json.dumps(
            {
                "intent": intent,
                "found": any(bool(row.get("relevant")) for row in rows),
                "judgments": rows,
            },
            ensure_ascii=False,
        ),
    )


async def _judge_batch(
    *,
    query: str,
    intent: str,
    batch_refs: Sequence[Mapping[str, Any]],
    model_key: Optional[str],
    user_id: Optional[int],
    fallback_candidates: Optional[List[str]],
    rubric: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Judge one batch.

    On protocol failure (invalid JSON / missing / dup / unknown / found conflict /
    schema): one full-batch repair, then targeted 补审 for still-missing keys only.
    Never rule-fills positives; never restarts the whole candidate pool.
    """
    annotated = annotate_with_canonical_keys(batch_refs)
    allowed = [str(r["reference_key"]) for r in annotated]
    by_key = {str(r["reference_key"]): r for r in annotated}
    user_prompt = _build_batch_user_prompt(
        query=query,
        intent=intent,
        annotated=annotated,
        allowed=allowed,
        rubric=rubric,
    )

    def _with_repair_meta(
        payload: Dict[str, Any],
        *,
        repair_count: int,
        missing_key_count: int,
        missing_key_recovered: bool,
    ) -> Dict[str, Any]:
        out = dict(payload)
        out["repair_count"] = int(repair_count)
        out["missing_key_count"] = int(missing_key_count)
        out["missing_key_recovered"] = bool(missing_key_recovered)
        return out

    raw = await _llm_judge_raw(
        user_prompt=user_prompt,
        model_key=model_key,
        user_id=user_id,
        fallback_candidates=fallback_candidates,
    )
    try:
        return _with_repair_meta(
            _validate_batch_payload(intent=intent, allowed=allowed, raw_payload=raw),
            repair_count=0,
            missing_key_count=0,
            missing_key_recovered=False,
        )
    except JudgeValidationError as exc:
        if exc.code == E_JUDGE_PROVIDER_FAILED or exc.code not in _REPAIRABLE_JUDGE_CODES:
            raise

        _, initial_missing_keys = _partial_judgments_from_raw(raw, allowed)
        initial_missing = len(initial_missing_keys) if initial_missing_keys else len(allowed)

        # Repair path 1: re-ask the full failed batch once (not the whole pool).
        repair_prompt = _build_batch_user_prompt(
            query=query,
            intent=intent,
            annotated=annotated,
            allowed=allowed,
            rubric=rubric,
            repair_note=(
                "上一次输出不合法或不完整（JSON/键覆盖/found 一致性）。"
                "请重新对本批每一个允许 key 各输出一条 judgment，仅输出 JSON。"
            ),
        )
        raw2 = await _llm_judge_raw(
            user_prompt=repair_prompt,
            model_key=model_key,
            user_id=user_id,
            fallback_candidates=fallback_candidates,
        )
        try:
            return _with_repair_meta(
                _validate_batch_payload(intent=intent, allowed=allowed, raw_payload=raw2),
                repair_count=1,
                missing_key_count=initial_missing,
                missing_key_recovered=True,
            )
        except JudgeValidationError as exc2:
            if exc2.code == E_JUDGE_PROVIDER_FAILED:
                raise
            # Repair path 2: targeted 补审 for still-missing keys only (LLM only).
            partial_rows, missing = _partial_judgments_from_raw(raw2, allowed)
            if not partial_rows:
                partial_rows, missing = _partial_judgments_from_raw(raw, allowed)
            if not partial_rows:
                return _with_repair_meta(
                    await _recover_batch_individually(
                        query=query,
                        intent=intent,
                        allowed=allowed,
                        by_key=by_key,
                        model_key=model_key,
                        user_id=user_id,
                        fallback_candidates=fallback_candidates,
                        rubric=rubric,
                    ),
                    repair_count=2,
                    missing_key_count=initial_missing,
                    missing_key_recovered=True,
                )
            if not missing:
                raise
            miss_refs = [by_key[k] for k in missing if k in by_key]
            if not miss_refs:
                raise
            miss_ann = annotate_with_canonical_keys(miss_refs)
            miss_prompt = _build_batch_user_prompt(
                query=query,
                intent=intent,
                annotated=miss_ann,
                allowed=missing,
                rubric=rubric,
                repair_note="补审：仅覆盖下列仍缺失的允许 key，不得省略；仅输出 JSON。",
            )
            raw3 = await _llm_judge_raw(
                user_prompt=miss_prompt,
                model_key=model_key,
                user_id=user_id,
                fallback_candidates=fallback_candidates,
            )
            try:
                repaired = _validate_batch_payload(
                    intent=intent, allowed=missing, raw_payload=raw3
                )
            except JudgeValidationError as exc3:
                if exc3.code == E_JUDGE_PROVIDER_FAILED:
                    raise
                repaired = await _recover_batch_individually(
                    query=query,
                    intent=intent,
                    allowed=missing,
                    by_key=by_key,
                    model_key=model_key,
                    user_id=user_id,
                    fallback_candidates=fallback_candidates,
                    rubric=rubric,
                )
            merged_rows: List[Dict[str, Any]] = list(partial_rows)
            seen = {str(r["reference_key"]) for r in merged_rows}
            for j in repaired.get("judgments") or []:
                key = str(j["reference_key"])
                if key in seen:
                    continue
                merged_rows.append(
                    {
                        "reference_key": key,
                        "relevant": bool(j.get("relevant")),
                        "reason_code": str(j.get("reason_code") or ""),
                    }
                )
                seen.add(key)
            return _with_repair_meta(
                _validate_batch_payload(
                    intent=intent,
                    allowed=allowed,
                    raw_payload=json.dumps(
                        {
                            "intent": intent,
                            "found": any(bool(j.get("relevant")) for j in merged_rows),
                            "judgments": merged_rows,
                        },
                        ensure_ascii=False,
                    ),
                ),
                repair_count=2,
                missing_key_count=len(missing),
                missing_key_recovered=True,
            )


def _merge_batch_results(
    *,
    intent: str,
    all_keys: Sequence[str],
    batch_results: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Merge per-batch validated results; re-check global coverage for lookup_all."""
    judgments: List[Dict[str, Any]] = []
    seen = set()
    found_votes: List[bool] = []
    for batch in batch_results:
        if not batch.get("ok"):
            raise JudgeValidationError("batch not ok", code=E_JUDGE_SCHEMA)
        if "found" not in batch or not isinstance(batch.get("found"), bool):
            raise JudgeValidationError(
                "lookup requires explicit boolean found per batch",
                code=E_JUDGE_FOUND_INVALID,
            )
        found_bool = bool(batch["found"])
        found_votes.append(found_bool)
        raw_j = batch.get("judgments")
        if not (isinstance(raw_j, list) and raw_j):
            raise JudgeValidationError(
                "batch missing judgments for merge",
                code=E_JUDGE_MISSING_KEY,
            )
        _assert_found_judgment_consistency(found=found_bool, judgments=raw_j)
        for row in raw_j:
            key = str(row.get("reference_key") or "").strip()
            if key in seen:
                raise JudgeValidationError(
                    f"duplicate across batches: {key}",
                    code=E_JUDGE_DUPLICATE_KEY,
                )
            seen.add(key)
            judgments.append(
                {
                    "reference_key": key,
                    "relevant": bool(row.get("relevant")),
                    "reason_code": str(row.get("reason_code") or ""),
                }
            )

    if intent in LOOKUP_INTENTS:
        missing = sorted(set(all_keys) - seen)
        if missing:
            raise JudgeValidationError(
                f"global coverage missing {len(missing)} keys",
                code=E_JUDGE_MISSING_KEY,
            )
        if len(seen) != len(all_keys):
            raise JudgeValidationError(
                f"coverage failed candidate={len(all_keys)} judged={len(seen)}",
                code=E_JUDGE_COVERAGE,
            )
        extras = sorted(seen - set(all_keys))
        if extras:
            raise JudgeValidationError(
                f"unknown keys in judgments: {len(extras)}",
                code=E_JUDGE_UNKNOWN_KEY,
            )

    matches = [j["reference_key"] for j in judgments if j["relevant"]]
    if not found_votes:
        raise JudgeValidationError(
            "lookup requires explicit boolean found",
            code=E_JUDGE_FOUND_INVALID,
        )
    # Aggregate LLM batch booleans only — never derive from match_count / metadata.
    found = any(found_votes)
    _assert_found_judgment_consistency(found=found, judgments=judgments)
    seen_eq = set(seen) == set(all_keys)
    repair_count = sum(int(b.get("repair_count") or 0) for b in batch_results)
    missing_key_count = sum(int(b.get("missing_key_count") or 0) for b in batch_results)
    missing_key_recovered = any(bool(b.get("missing_key_recovered")) for b in batch_results)

    return {
        "ok": True,
        "intent": intent,
        "temperature": JUDGE_TEMPERATURE,
        "candidate_count": len(all_keys),
        "judged_count": len(judgments),
        "match_count": len(matches),
        "matches": matches,
        "judgments": judgments,
        "found": found,
        "batch_count": len(batch_results),
        "must_not_treat_as_zero_hits": False,
        "llm_only": True,
        # Only true when every candidate key was judged — never unconditional for lookup_one.
        "llm_judged_all_candidates": seen_eq,
        "repair_count": repair_count,
        "missing_key_count": missing_key_count,
        "missing_key_recovered": missing_key_recovered,
    }


def _has_generic_support_signal(ref: Mapping[str, Any]) -> bool:
    """Evidence hints only — never decides relevant; used to schedule LLM revisit."""
    ev = ref.get("judge_evidence") if isinstance(ref.get("judge_evidence"), dict) else {}
    if ev.get("title_term_hit") or ev.get("body_term_hit"):
        return True
    for key in ("title_term_hit_count", "body_term_hit_count", "term_hit_count"):
        val = ev.get(key)
        if isinstance(val, int) and val > 0:
            return True
    return False


def _ensure_evidence_digests(
    by_key: Mapping[str, Mapping[str, Any]],
) -> Dict[str, str]:
    digests: Dict[str, str] = {}
    for key, ref in by_key.items():
        ev = ref.get("judge_evidence") if isinstance(ref.get("judge_evidence"), dict) else {}
        if not isinstance(ev, dict):
            ev = {}
            ref["judge_evidence"] = ev  # type: ignore[index]
        digest = str(ev.get("evidence_digest") or "").strip() or compute_evidence_digest(ev)
        ev["evidence_digest"] = digest
        digests[str(key)] = digest
    return digests


async def _llm_single_candidate_adjudicate(
    *,
    query: str,
    intent: str,
    ref: Mapping[str, Any],
    model_key: Optional[str],
    user_id: Optional[int],
    fallback_candidates: Optional[List[str]],
    rubric: Optional[Mapping[str, Any]] = None,
    pass_a_row: Optional[Mapping[str, Any]] = None,
    pass_b_row: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Third-round single-candidate LLM decision with A/B context + fixed evidence."""
    annotated = annotate_with_canonical_keys([ref])
    key = str(annotated[0]["reference_key"])
    evidence_blob = _snippet_for_ref(annotated[0])
    rubric_block = f"relevance_rubric: {_format_rubric(rubric)}\n" if rubric else ""
    a_rel = bool((pass_a_row or {}).get("relevant")) if pass_a_row else None
    b_rel = bool((pass_b_row or {}).get("relevant")) if pass_b_row else None
    decision_context = (
        "Pass A 与 Pass B 不一致，请做最终裁决。"
        if pass_a_row is not None or pass_b_row is not None
        else "此前整批结构化输出不可用，请独立裁决这个候选。"
    )
    user_prompt = (
        f"intent={intent}\n"
        f"用户问题: {query}\n"
        f"{rubric_block}"
        f"允许的 reference_key: {json.dumps([key], ensure_ascii=False)}\n"
        f"reference_key={key}\n"
        f"pass_a: relevant={a_rel} reason_code={(pass_a_row or {}).get('reason_code')}\n"
        f"pass_b: relevant={b_rel} reason_code={(pass_b_row or {}).get('reason_code')}\n"
        f"固定证据:\n{evidence_blob}\n"
        f"{decision_context}请仅基于证据与量规做最终裁决，"
        "必须对本允许 key 给出且仅一条 judgment。"
        "输出严格 JSON："
        '{"intent":"'
        + intent
        + '","found":true|false,"judgments":[{"reference_key":"'
        + key
        + '","relevant":true|false,"reason_code":"direct_topic|semantic_equivalent|incidental|unrelated"}]}'
    )
    raw = await _llm_judge_raw(
        user_prompt=user_prompt,
        model_key=model_key,
        user_id=user_id,
        fallback_candidates=fallback_candidates,
        prefer_json_mode=True,
        max_tokens=600,
    )
    try:
        payload = _validate_batch_payload(
            intent=intent,
            allowed=[key],
            raw_payload=raw,
        )
    except JudgeValidationError as exc:
        if exc.code == E_JUDGE_PROVIDER_FAILED or exc.code not in _REPAIRABLE_JUDGE_CODES:
            raise
        repair_prompt = (
            user_prompt
            + "\n上一次单候选输出不合法。请仅输出一个完整 JSON 对象，"
            "不得使用 markdown，不得省略允许的 key。"
        )
        raw2 = await _llm_judge_raw(
            user_prompt=repair_prompt,
            model_key=model_key,
            user_id=user_id,
            fallback_candidates=fallback_candidates,
            prefer_json_mode=True,
            max_tokens=600,
            temperature=0.0,
        )
        payload = _validate_batch_payload(
            intent=intent,
            allowed=[key],
            raw_payload=raw2,
        )
    row = payload["judgments"][0]
    return {
        "reference_key": key,
        "relevant": row["relevant"],
        "reason_code": str(row.get("reason_code") or ""),
        "found": payload["found"],
    }


async def _judge_single_pass(
    *,
    query: str,
    intent_n: str,
    keys: Sequence[str],
    by_key: Mapping[str, Mapping[str, Any]],
    model_key: Optional[str],
    user_id: Optional[int],
    fallback_candidates: Optional[List[str]],
    batch_size: Optional[int],
    rubric: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    size = batch_size or _batch_size()
    batches = _chunk_keys(keys, size)
    batch_results: List[Dict[str, Any]] = []
    for batch_keys in batches:
        # Both lookup intents require full per-batch coverage.
        batch_intent = intent_n if intent_n in LOOKUP_INTENTS else INTENT_LOOKUP_ALL
        batch_refs = [by_key[k] for k in batch_keys]
        result = await _judge_batch(
            query=query,
            intent=batch_intent,
            batch_refs=batch_refs,
            model_key=model_key,
            user_id=user_id,
            fallback_candidates=fallback_candidates,
            rubric=rubric,
        )
        if "judgments" not in result or not isinstance(result.get("judgments"), list):
            raise JudgeValidationError(
                "batch missing judgments",
                code=E_JUDGE_MISSING_KEY,
            )
        batch_results.append(result)
    return _merge_batch_results(
        intent=intent_n,
        all_keys=keys,
        batch_results=batch_results,
    )


async def _finalize_found_from_llm(
    *,
    query: str,
    intent_n: str,
    matches: Sequence[str],
    final_judgments: Sequence[Mapping[str, Any]],
    model_key: Optional[str],
    user_id: Optional[int],
    fallback_candidates: Optional[List[str]],
    rubric: Optional[Mapping[str, Any]] = None,
) -> bool:
    rubric_block = f"relevance_rubric: {_format_rubric(rubric)}\n" if rubric else ""
    found_prompt = (
        f"intent={intent_n}\n用户问题: {query}\n"
        f"{rubric_block}"
        f"最终 relevant keys: {json.dumps(list(matches), ensure_ascii=False)}\n"
        f"最终 judgments: {json.dumps(list(final_judgments), ensure_ascii=False)}\n"
        "请只输出 JSON 对象：{\"found\": true} 或 {\"found\": false}。"
        "found=true 当且仅当至少一条 relevant=true。"
    )
    try:
        llm = await generate_chat_completion(
            model_key=model_key,
            system="你只输出严格 JSON：{\"found\":true|false}。",
            messages=[{"role": "user", "content": found_prompt}],
            mode="retrieval",
            max_tokens=32,
            temperature=JUDGE_TEMPERATURE,
            user_id=user_id,
            fallback_candidates=fallback_candidates,
        )
        raw = json.loads((llm.content or "").strip())
        if not isinstance(raw, dict) or not isinstance(raw.get("found"), bool):
            raise JudgeValidationError(
                "found finalize missing boolean",
                code=E_JUDGE_FOUND_INVALID,
            )
        return bool(raw["found"])
    except JudgeValidationError:
        raise
    except Exception as exc:
        raise JudgeValidationError(
            f"found finalize failed: {type(exc).__name__}",
            code=E_JUDGE_PROVIDER_FAILED,
        ) from exc


def _reason_from_positive(*rows: Optional[Mapping[str, Any]]) -> str:
    for row in rows:
        if row and bool(row.get("relevant")):
            return str(row.get("reason_code") or "direct_topic")
    for row in rows:
        if row:
            return str(row.get("reason_code") or "unrelated")
    return "unrelated"


async def _apply_pass_b_merge(
    *,
    query: str,
    intent_n: str,
    keys: Sequence[str],
    by_key: Mapping[str, Mapping[str, Any]],
    pass_a: Mapping[str, Any],
    revisit_keys: Sequence[str],
    model_key: Optional[str],
    user_id: Optional[int],
    fallback_candidates: Optional[List[str]],
    batch_size: Optional[int],
    rubric: Optional[Mapping[str, Any]],
    strategy_label: str,
) -> Dict[str, Any]:
    a_by_key = {str(j["reference_key"]): j for j in (pass_a.get("judgments") or [])}
    if not revisit_keys:
        out = dict(pass_a)
        out["strategy"] = strategy_label
        if rubric:
            out["relevance_rubric"] = dict(rubric)
        return out

    pass_b_keys = list(revisit_keys)
    pass_b = await _judge_single_pass(
        query=query,
        intent_n=intent_n,
        keys=pass_b_keys,
        by_key=by_key,
        model_key=model_key,
        user_id=user_id,
        fallback_candidates=fallback_candidates,
        batch_size=batch_size,
        rubric=rubric,
    )
    b_by_key = {str(j["reference_key"]): j for j in (pass_b.get("judgments") or [])}
    final_judgments: List[Dict[str, Any]] = []
    third_count = 0
    candidate_trace: Dict[str, Any] = {}
    recall_first = intent_n == INTENT_LOOKUP_ALL
    for k in keys:
        a_row = a_by_key[k]
        b_row = b_by_key.get(k)
        third_row = None
        a_rel = bool(a_row.get("relevant"))
        b_rel = bool(b_row.get("relevant")) if b_row is not None else False
        if b_row is None:
            final = dict(a_row)
        elif recall_first:
            # R5.5E lookup_all: keep if A∨B; third on disagreement for diagnosis /
            # supplemental positives, but cannot veto A/B positives.
            if a_rel != b_rel:
                third = await _llm_single_candidate_adjudicate(
                    query=query,
                    intent=intent_n,
                    ref=by_key[k],
                    model_key=model_key,
                    user_id=user_id,
                    fallback_candidates=fallback_candidates,
                    rubric=rubric,
                    pass_a_row=a_row,
                    pass_b_row=b_row,
                )
                third_count += 1
                third_rel = bool(third.get("relevant"))
                third_row = {
                    "relevant": third_rel,
                    "reason_code": str(third.get("reason_code") or ""),
                }
                keep = bool(a_rel or b_rel or third_rel)
                final = {
                    "reference_key": k,
                    "relevant": keep,
                    "reason_code": (
                        _reason_from_positive(a_row, b_row, third_row)
                        if keep
                        else str(third.get("reason_code") or "unrelated")
                    ),
                }
            else:
                # Agree true → keep; agree false → exclude (no third).
                final = {
                    "reference_key": k,
                    "relevant": bool(a_rel),
                    "reason_code": str(
                        (a_row if a_rel else b_row).get("reason_code") or ""
                    ),
                }
        elif a_rel == b_rel:
            final = dict(b_row)
        else:
            third = await _llm_single_candidate_adjudicate(
                query=query,
                intent=intent_n,
                ref=by_key[k],
                model_key=model_key,
                user_id=user_id,
                fallback_candidates=fallback_candidates,
                rubric=rubric,
                pass_a_row=a_row,
                pass_b_row=b_row,
            )
            third_count += 1
            third_row = {
                "relevant": bool(third.get("relevant")),
                "reason_code": str(third.get("reason_code") or ""),
            }
            final = {
                "reference_key": k,
                "relevant": bool(third.get("relevant")),
                "reason_code": str(third.get("reason_code") or ""),
            }
        final_judgments.append(final)
        ev = by_key[k].get("judge_evidence") if isinstance(by_key[k].get("judge_evidence"), dict) else {}
        candidate_trace[k] = {
            "pass_a": {
                "relevant": bool(a_row.get("relevant")),
                "reason_code": str(a_row.get("reason_code") or ""),
            },
            "pass_b": (
                {
                    "relevant": bool(b_row.get("relevant")),
                    "reason_code": str(b_row.get("reason_code") or ""),
                }
                if b_row is not None
                else None
            ),
            "third": third_row,
            "final": {
                "relevant": bool(final.get("relevant")),
                "reason_code": str(final.get("reason_code") or ""),
            },
            "evidence_mode": str(ev.get("evidence_mode") or ""),
            "evidence_coverage": ev.get("evidence_coverage"),
            "truncated": bool(ev.get("truncated")),
            "window_count": int(ev.get("window_count") or 0),
            "evidence_digest": str(ev.get("evidence_digest") or ""),
        }

    matches = [j["reference_key"] for j in final_judgments if j["relevant"]]
    final_by_key = {str(j["reference_key"]): j for j in final_judgments}
    if recall_first:
        # found follows LLM judgment union (never keyword-filled).
        found = any(bool(j.get("relevant")) for j in final_judgments)
        _assert_found_judgment_consistency(found=found, judgments=final_judgments)
    else:
        flipped = any(
            bool(a_by_key[k].get("relevant")) != bool(final_by_key[k].get("relevant"))
            for k in keys
        )
        if not flipped:
            found = bool(pass_a.get("found"))
            _assert_found_judgment_consistency(found=found, judgments=final_judgments)
        else:
            found = await _finalize_found_from_llm(
                query=query,
                intent_n=intent_n,
                matches=matches,
                final_judgments=final_judgments,
                model_key=model_key,
                user_id=user_id,
                fallback_candidates=fallback_candidates,
                rubric=rubric,
            )
            _assert_found_judgment_consistency(found=found, judgments=final_judgments)

    seen_keys = {j["reference_key"] for j in final_judgments}
    repair_count = int(pass_a.get("repair_count") or 0) + int(pass_b.get("repair_count") or 0)
    missing_key_count = int(pass_a.get("missing_key_count") or 0) + int(
        pass_b.get("missing_key_count") or 0
    )
    missing_key_recovered = bool(pass_a.get("missing_key_recovered")) or bool(
        pass_b.get("missing_key_recovered")
    )
    out: Dict[str, Any] = {
        "ok": True,
        "intent": intent_n,
        "temperature": JUDGE_TEMPERATURE,
        "candidate_count": len(keys),
        "judged_count": len(final_judgments),
        "match_count": len(matches),
        "matches": matches,
        "judgments": final_judgments,
        "found": found,
        "batch_count": int(pass_a.get("batch_count") or 0)
        + int(pass_b.get("batch_count") or 0),
        "must_not_treat_as_zero_hits": False,
        "llm_only": True,
        "llm_judged_all_candidates": seen_keys == set(keys),
        "strategy": strategy_label,
        "strategy_revisit_count": len(revisit_keys),
        "strategy_third_count": third_count,
        "recall_first_union": recall_first,
        "repair_count": repair_count,
        "missing_key_count": missing_key_count,
        "missing_key_recovered": missing_key_recovered,
    }
    if rubric:
        out["relevance_rubric"] = dict(rubric)
    # Preserve digests for audit (hashes only).
    out["evidence_digests"] = {
        k: str((by_key[k].get("judge_evidence") or {}).get("evidence_digest") or "")
        for k in keys
    }
    out["candidate_trace"] = candidate_trace
    out["pass_b_key_order"] = list(pass_b_keys)
    out["pass_b_covers_all"] = set(pass_b_keys) == set(keys)
    return out


async def judge_lookup_candidates(
    *,
    query: str,
    intent: str,
    candidates: Sequence[Mapping[str, Any]],
    model_key: Optional[str] = None,
    user_id: Optional[int] = None,
    fallback_candidates: Optional[List[str]] = None,
    batch_size: Optional[int] = None,
    strategy: Optional[str] = None,
    relevance_rubric: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Full-pool LLM-only judgment.

    Strategy A: single pass.
    Strategy B: pass A + revisit false candidates with generic support signals.
    Strategy C: shared rubric (canonical semantics) + pass A full + pass B full
    (deterministic reshuffle/grouping) + third LLM on disagreement.
    lookup_all: recall-first union (A∨B; third cannot veto positives).
    lookup_one/answer: third-wins precision semantics.
    Server only schedules/validates; final relevant always from LLM.

    If relevance_rubric is provided (already validated), Strategy C reuses it
    and does not call the rubric LLM again (used by stability_runs).
    """
    try:
        intent_n = validate_intent(intent)
    except JudgeValidationError as exc:
        return judge_failure_from_exception(exc)

    annotated = annotate_with_canonical_keys(candidates)
    keys = [str(r["reference_key"]) for r in annotated]
    by_key = {str(r["reference_key"]): r for r in annotated}
    digests = _ensure_evidence_digests(by_key) if keys else {}
    canonical_semantics = build_canonical_lookup_semantics(query, intent=intent_n)

    if not keys:
        return {
            "ok": True,
            "intent": intent_n,
            "temperature": JUDGE_TEMPERATURE,
            "candidate_count": 0,
            "judged_count": 0,
            "match_count": 0,
            "matches": [],
            "judgments": [],
            "found": False,
            "batch_count": 0,
            "must_not_treat_as_zero_hits": False,
            "empty_candidate_pool": True,
            "llm_only": True,
            "llm_judged_all_candidates": True,
            "strategy": strategy or _judge_strategy(),
            "canonical_lookup_semantics": canonical_semantics,
        }

    strat = (strategy or _judge_strategy()).upper()
    if strat not in {"A", "B", "C"}:
        strat = "C"

    try:
        rubric: Optional[Dict[str, Any]] = None
        if strat == "C":
            if relevance_rubric is not None:
                rubric = _validate_relevance_rubric(relevance_rubric)
            else:
                rubric = await _generate_relevance_rubric(
                    query=query,
                    model_key=model_key,
                    user_id=user_id,
                    fallback_candidates=fallback_candidates,
                    intent=intent_n,
                    canonical_semantics=canonical_semantics,
                )

        pass_a = await _judge_single_pass(
            query=query,
            intent_n=intent_n,
            keys=keys,
            by_key=by_key,
            model_key=model_key,
            user_id=user_id,
            fallback_candidates=fallback_candidates,
            batch_size=batch_size,
            rubric=rubric,
        )
        pass_a["strategy"] = strat
        if rubric:
            pass_a["relevance_rubric"] = dict(rubric)
        pass_a["evidence_digests"] = digests
        pass_a["canonical_lookup_semantics"] = canonical_semantics
        # Digest stability: same candidate must keep same digest across this request.
        for k in keys:
            ev = by_key[k].get("judge_evidence") or {}
            if str(ev.get("evidence_digest") or "") != digests[k]:
                raise JudgeValidationError(
                    "evidence_digest mutated during judge",
                    code=E_JUDGE_SCHEMA,
                )

        if strat == "A":
            return pass_a

        a_by_key = {
            str(j["reference_key"]): j for j in (pass_a.get("judgments") or [])
        }
        if strat == "B":
            revisit_keys = [
                k
                for k in keys
                if (not bool((a_by_key.get(k) or {}).get("relevant")))
                and _has_generic_support_signal(by_key[k])
            ]
        else:
            # R5.5D Strategy C: Pass B covers ALL candidates (no support-signal filter).
            revisit_keys = _pass_b_key_order(keys)

        out = await _apply_pass_b_merge(
            query=query,
            intent_n=intent_n,
            keys=keys,
            by_key=by_key,
            pass_a=pass_a,
            revisit_keys=revisit_keys,
            model_key=model_key,
            user_id=user_id,
            fallback_candidates=fallback_candidates,
            batch_size=batch_size,
            rubric=rubric,
            strategy_label=strat,
        )
        out["canonical_lookup_semantics"] = canonical_semantics
        return out
    except JudgeValidationError as exc:
        logger.warning(
            "lookup judge failed code=%s detail=%s",
            exc.code,
            str(exc)[:160],
        )
        return judge_failure_from_exception(exc)
    except LLMGatewayError as exc:
        return judge_failure_from_exception(exc)
    except Exception as exc:
        return judge_failure_from_exception(exc)


def estimate_batch_count(candidate_count: int, batch_size: Optional[int] = None) -> int:
    size = batch_size or _batch_size()
    if candidate_count <= 0:
        return 0
    return int(math.ceil(candidate_count / float(size)))


async def stability_runs(
    *,
    query: str,
    intent: str,
    candidates: Sequence[Mapping[str, Any]],
    repeats: int = 3,
    model_key: Optional[str] = None,
    user_id: Optional[int] = None,
    strategy: Optional[str] = None,
    metric_fn: Optional[Any] = None,
    precision_floor: float = 0.70,
) -> Dict[str, Any]:
    """Repeat full LLM judgment.

    exact-set agreement / pairwise Jaccard are diagnostics.
    lookup_all quality_pass = every successful run meets Recall/Precision/coverage/found
    when metric_fn is provided (gold-aware). Protocol errors prevent quality_pass.
    """
    prev_cache = os.environ.get("RAG_LLM_RESULT_CACHE")
    os.environ["RAG_LLM_RESULT_CACHE"] = "0"
    runs: List[List[str]] = []
    errors: List[str] = []
    run_metrics: List[Dict[str, Any]] = []
    shared_rubric: Optional[Dict[str, Any]] = None
    strat = (strategy or _judge_strategy()).upper()
    try:
        if strat == "C":
            shared_rubric = await _generate_relevance_rubric(
                query=query,
                model_key=model_key,
                user_id=user_id,
                fallback_candidates=None,
                intent=intent,
                canonical_semantics=build_canonical_lookup_semantics(query, intent=intent),
            )
        for _ in range(max(1, repeats)):
            result = await judge_lookup_candidates(
                query=query,
                intent=intent,
                candidates=candidates,
                model_key=model_key,
                user_id=user_id,
                strategy=strat,
                relevance_rubric=shared_rubric,
            )
            if not result.get("ok"):
                code = str(result.get("error_code") or "failed")
                errors.append(code)
                run_metrics.append(
                    {
                        "ok": False,
                        "error_code": code,
                        "coverage_ok": False,
                        "found": None,
                        "match_recall_at_gold": 0.0,
                        "match_precision_at_gold": 0.0,
                        "protocol_error": True,
                    }
                )
                continue
            matches = list(result.get("matches") or [])
            runs.append(matches)
            base_metric: Dict[str, Any] = {
                "ok": True,
                "error_code": None,
                "coverage_ok": int(result.get("candidate_count") or -1)
                == int(result.get("judged_count") or -2),
                "found": bool(result.get("found")),
                "candidate_count": result.get("candidate_count"),
                "judged_count": result.get("judged_count"),
                "match_count": result.get("match_count"),
                "protocol_error": False,
            }
            if callable(metric_fn):
                extra = metric_fn(result) or {}
                base_metric.update(dict(extra))
            run_metrics.append(base_metric)
    finally:
        if prev_cache is None:
            os.environ.pop("RAG_LLM_RESULT_CACHE", None)
        else:
            os.environ["RAG_LLM_RESULT_CACHE"] = prev_cache
    from app.eval.r5_schema import (
        pairwise_jaccard,
        stability_agreement,
        stability_quality_pass,
    )

    quality_ok = False
    if run_metrics and all(not m.get("protocol_error") for m in run_metrics):
        if any("match_recall_at_gold" in m for m in run_metrics):
            quality_ok = stability_quality_pass(
                run_metrics, precision_floor=precision_floor
            ) and len(run_metrics) == repeats
        else:
            # No gold metrics attached — coverage/found only.
            quality_ok = all(
                bool(m.get("ok"))
                and bool(m.get("coverage_ok"))
                and m.get("found") is True
                for m in run_metrics
            ) and len(run_metrics) == repeats

    return {
        "repeat_count": repeats,
        "successful_runs": len(runs),
        "agreement": stability_agreement(runs) if runs else 0.0,
        "pairwise_jaccard": pairwise_jaccard(runs) if runs else 0.0,
        "agreement_is_diagnostic": True,
        "quality_pass": quality_ok,
        "run_metrics": run_metrics,
        "errors": errors,
        "temperature": JUDGE_TEMPERATURE,
        "llm_only": True,
        "cache_disabled": True,
        "rubric_generated_once": bool(shared_rubric is not None),
        "relevance_rubric": dict(shared_rubric) if shared_rubric else None,
    }
