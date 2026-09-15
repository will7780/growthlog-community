"""Bounded semantic synthesis for the GrowthLog retriever.

The retriever keeps the high-recall Hybrid pool, applies a deterministic
channel-relative RAG admission gate, then sends only the admitted bounded
summaries to one LLM turn that selects, summarizes, and cites relevant sources.
A second physical call is allowed only to repair the public citation contract,
never to make a new relevance decision. Provider or protocol failures degrade
to admitted candidates with explicit user-facing fallback copy.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from app.services.llm_gateway import LLMGatewayError, generate_chat_completion

logger = logging.getLogger(__name__)

LEVEL_CORE = 4
LEVEL_STRONG = 3
LEVEL_USEFUL = 2
LEVEL_WEAK = 1
LEVEL_IRRELEVANT = 0
RELEVANCE_THRESHOLD = 2

DEFAULT_SELECTION_CAP = 40
DEFAULT_HYBRID_FALLBACK_CAP = 12
DEFAULT_SELECTION_INPUT_CAP = 48
DEFAULT_SELECTION_INPUT_CHAR_BUDGET = 14000
MAX_HIT_WINDOW_CHARS = 140
RAG_ADMISSION_ABSOLUTE_FLOOR = 0.35
RAG_ADMISSION_DENSE_RATIO = 0.985
RAG_ADMISSION_ATTACHMENT_RATIO = 0.98
RAG_ADMISSION_GLOBAL_MIN = 8
RAG_ADMISSION_MIN_SEATS = {
    "entry_dense": 6,
    "knowledge_dense": 3,
    "attachment_dense": 3,
}
_RAG_DENSE_CHANNELS = tuple(RAG_ADMISSION_MIN_SEATS)
_RAG_ADMISSION_CHANNELS = (
    "lexical_title",
    "lexical_body",
    *_RAG_DENSE_CHANNELS,
    "other",
)

NO_RELEVANT_SOURCES = "__NO_RELEVANT_SOURCES__"
MAX_SYNTHESIS_OUTPUT_CHARS = 12000
_ALIAS_CITATION_RE = re.compile(r"〔(S[1-9]\d*)〕")
_ANY_ALIAS_RE = re.compile(r"\bS[1-9]\d*\b")
_COMMON_ALIAS_CITATION_RE = re.compile(
    r"(?:\[|【|［)\s*(S[1-9]\d*)\s*(?:\]|】|］)"
)

SYNTHESIS_SYSTEM = """你是 GrowthLog 检索鼠。你必须在一次回答里完成来源的语义筛选、重要度排序和简明归纳。

规则：
1. 只保留真正能帮助用户请求的候选。标题偶然含词、正文主题不同的候选必须排除。
2. 直接回答用户要找什么，并用简洁列表说明每条来源实际提供了什么；不要输出内部筛选过程或分数。
3. 每个结论或条目句末必须引用候选 alias，格式只能是 〔S1〕。可以一条结论引用多个候选。
4. 只能引用本次候选中的 alias。禁止输出数据库 ID、reference_key、chunk_id、文件路径、模型名或内部字段。
5. 展示模式只影响详略，不改变相关性边界。find_one 优先少量最相关来源；list_all 尽可能覆盖全部实质相关来源；ordinary 直接回应用户问题。
6. 如果全部候选都没有实质关系，只输出 __NO_RELEVANT_SOURCES__。
7. 不输出 JSON、代码块、候选 alias 清单或额外协议说明，只输出最终给用户看的中文答案。
"""

CITATION_REPAIR_SYSTEM = """你只负责修复 GrowthLog 检索鼠答案的引用契约，不得添加候选之外的事实。
保留原答案的语义取舍和简明表达；每个保留的结论句末改用允许的 〔S#〕 引用。
只能使用提供的 alias。若原答案实际判断没有相关来源，只输出 __NO_RELEVANT_SOURCES__。
禁止输出 JSON、代码块、数据库 ID、reference_key、chunk_id、路径、模型名或解释。
删除项目符号开头的 S1/S2 等 alias 标签，只在句末保留 〔S#〕 引用。
"""

_INTERNAL_REASON_RE = re.compile(
    r"(?:reference_key|chunk_id|entry_id|attachment_id|source_id|todo_id|root_todo_id|family_root_todo_id|"
    r"[A-Za-z]:\\|/app/|/opt/|\\uploads\\|/uploads/)",
    re.IGNORECASE,
)


def _selection_cap() -> int:
    raw = (os.getenv("RAG_RELEVANCE_ANSWER_EXPAND_CAP") or "").strip()
    if raw.isdigit():
        return max(8, min(80, int(raw)))
    return DEFAULT_SELECTION_CAP


def _fallback_cap() -> int:
    raw = (os.getenv("RAG_RETRIEVER_FALLBACK_TOP_K") or "").strip()
    if raw.isdigit():
        return max(4, min(30, int(raw)))
    return DEFAULT_HYBRID_FALLBACK_CAP


def _selection_input_cap() -> int:
    raw = (os.getenv("RAG_RETRIEVER_SELECTION_INPUT_CAP") or "").strip()
    if raw.isdigit():
        return max(8, min(64, int(raw)))
    return DEFAULT_SELECTION_INPUT_CAP


def _selection_input_char_budget() -> int:
    raw = (os.getenv("RAG_RETRIEVER_SELECTION_CHAR_BUDGET") or "").strip()
    if raw.isdigit():
        return max(4000, min(24000, int(raw)))
    return DEFAULT_SELECTION_INPUT_CHAR_BUDGET


def presentation_mode_from_intent(intent: str) -> str:
    return {
        "lookup_one": "find_one",
        "lookup_all": "list_all",
        "answer": "ordinary",
    }.get(str(intent or ""), "ordinary")


def retrieval_topic_fingerprint(topic_terms: Sequence[str]) -> str:
    terms = sorted({str(term).strip().lower() for term in topic_terms if str(term).strip()})
    return hashlib.sha256("|".join(terms).encode("utf-8")).hexdigest()[:24]


def candidate_pool_fingerprint(reference_keys: Sequence[str]) -> str:
    keys = [str(key) for key in reference_keys if str(key).strip()]
    return hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()[:32]


def ranking_fingerprint(reference_keys: Sequence[str]) -> str:
    keys = [str(key) for key in reference_keys if str(key).strip()]
    return hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()[:32]


# Compatibility name used by older diagnostics. New code should use ranking_fingerprint.
candidate_order_fingerprint = ranking_fingerprint


def _safe_bool(value: Any) -> bool:
    return value if isinstance(value, bool) else False


def _safe_nonneg_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except Exception:
        return 0


def _safe_coverage(value: Any) -> float:
    try:
        number = float(value)
    except Exception:
        return 0.0
    if number != number or number in (float("inf"), float("-inf")):
        return 0.0
    return min(1.0, max(0.0, number))


def _safe_similarity(value: Any) -> float:
    try:
        number = float(value)
    except Exception:
        return 0.0
    if number != number or number in (float("inf"), float("-inf")):
        return 0.0
    return max(0.0, number)


def _rag_admission_channel(ref: Mapping[str, Any]) -> str:
    """Classify one candidate without exposing identity or query content."""
    evidence = (
        ref.get("judge_evidence")
        if isinstance(ref.get("judge_evidence"), dict)
        else {}
    )
    if _safe_bool(evidence.get("title_term_hit")):
        return "lexical_title"
    if _safe_bool(evidence.get("body_term_hit")):
        return "lexical_body"

    method = str(ref.get("retrieval_method") or "").lower()
    source_type = str(ref.get("source_type") or "").lower()
    if source_type == "attachment_chunk" or method == "attachment_dense":
        return "attachment_dense"
    if source_type in {"knowledge_source", "knowledge"} or method == "knowledge_dense":
        return "knowledge_dense"
    if method in {"dense", "entry_dense"} or source_type == "entry":
        return "entry_dense"
    return "other"


def select_rag_admission_summaries(
    candidates: Sequence[Mapping[str, Any]],
    summaries: Sequence[Mapping[str, Any]],
    *,
    apply_recall_floors: bool = True,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Apply the pre-LLM RAG gate while preserving recall floors.

    Direct lexical hits are admitted. Dense-only candidates must clear their
    own channel-relative threshold. Retriever callers retain deterministic
    per-channel/global recall floors by default; explainer nomination disables
    those hit-level floors and applies its three-family floor after dedupe.
    The returned order remains the original candidate order.
    """
    if len(candidates) != len(summaries):
        raise ValueError("rag_admission_length_mismatch")

    channel_by_index: Dict[int, str] = {}
    channel_input = {name: 0 for name in _RAG_ADMISSION_CHANNELS}
    dense_buckets: Dict[str, List[Tuple[int, float]]] = {
        name: [] for name in _RAG_DENSE_CHANNELS
    }
    admitted_indexes = set()

    for index, ref in enumerate(candidates):
        channel = _rag_admission_channel(ref)
        channel_by_index[index] = channel
        channel_input[channel] = channel_input.get(channel, 0) + 1
        if channel in {"lexical_title", "lexical_body"}:
            admitted_indexes.add(index)
        elif channel in dense_buckets:
            dense_buckets[channel].append(
                (index, _safe_similarity(ref.get("relevance_score")))
            )

    thresholds: Dict[str, float] = {}
    floor_added_count = 0
    for channel, bucket in dense_buckets.items():
        if not bucket:
            continue
        ratio = (
            RAG_ADMISSION_ATTACHMENT_RATIO
            if channel == "attachment_dense"
            else RAG_ADMISSION_DENSE_RATIO
        )
        top_score = max(score for _index, score in bucket)
        threshold = max(RAG_ADMISSION_ABSOLUTE_FLOOR, top_score * ratio)
        thresholds[channel] = round(threshold, 6)
        ranked_bucket = sorted(bucket, key=lambda item: (-item[1], item[0]))
        for index, score in ranked_bucket:
            if score >= threshold:
                admitted_indexes.add(index)
        if apply_recall_floors:
            for index, _score in ranked_bucket[: RAG_ADMISSION_MIN_SEATS[channel]]:
                if index not in admitted_indexes:
                    floor_added_count += 1
                    admitted_indexes.add(index)

    global_floor_added_count = 0
    minimum = min(RAG_ADMISSION_GLOBAL_MIN, len(candidates))
    if apply_recall_floors and len(admitted_indexes) < minimum:
        for index in range(len(candidates)):
            if index in admitted_indexes:
                continue
            admitted_indexes.add(index)
            global_floor_added_count += 1
            if len(admitted_indexes) >= minimum:
                break

    selected = [
        dict(summaries[index])
        for index in range(len(summaries))
        if index in admitted_indexes
    ]
    channel_admitted = {name: 0 for name in _RAG_ADMISSION_CHANNELS}
    for index in admitted_indexes:
        channel = channel_by_index.get(index, "other")
        channel_admitted[channel] = channel_admitted.get(channel, 0) + 1

    return selected, {
        "input_count": len(candidates),
        "admitted_count": len(selected),
        "rejected_count": max(0, len(candidates) - len(selected)),
        "channel_input": channel_input,
        "channel_admitted": channel_admitted,
        "channel_thresholds": thresholds,
        "dense_ratio": RAG_ADMISSION_DENSE_RATIO,
        "attachment_ratio": RAG_ADMISSION_ATTACHMENT_RATIO,
        "absolute_floor": RAG_ADMISSION_ABSOLUTE_FLOOR,
        "recall_floors_enabled": bool(apply_recall_floors),
        "floor_added_count": floor_added_count,
        "global_floor_added_count": global_floor_added_count,
    }


def _sanitize_public_title(title: str) -> str:
    text = re.sub(r"\s+", " ", str(title or "")).strip()
    if re.fullmatch(r"记录\s*#\s*\d+", text):
        return "记录"
    text = re.sub(r"记录\s*#\s*\d+", "记录", text)
    return text[:120]


def _safe_title(ref: Mapping[str, Any]) -> str:
    evidence = ref.get("judge_evidence") if isinstance(ref.get("judge_evidence"), dict) else {}
    title = str(evidence.get("title") or ref.get("title") or "")
    if not title:
        content = str(ref.get("content") or ref.get("snippet") or "")
        title = content.split("\n", 1)[0].strip()
    sanitized = _sanitize_public_title(title)
    if _INTERNAL_REASON_RE.search(sanitized):
        return "相关记录"
    return sanitized or "未命名记录"


def _redact_internal_text(value: Any, *, maximum: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return ""
    text = re.sub(
        r"\b(?:reference_key|chunk_id|entry_id|attachment_id|source_id|todo_id|root_todo_id|family_root_todo_id)\s*[:=]\s*\S+",
        "[内部字段已省略]",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"(?:[A-Za-z]:\\|/app/|/opt/|/uploads/)\S+", "[路径已省略]", text)
    return text[:maximum]


def _safe_hit_window(ref: Mapping[str, Any]) -> str:
    evidence = ref.get("judge_evidence") if isinstance(ref.get("judge_evidence"), dict) else {}
    return _redact_internal_text(
        evidence.get("hit_window") or ref.get("snippet") or "",
        maximum=MAX_HIT_WINDOW_CHARS,
    )


def _safe_labels(ref: Mapping[str, Any]) -> List[str]:
    labels = (
        ref.get("labels")
        or ref.get("label_names")
        or ([ref.get("label_name")] if ref.get("label_name") else [])
    )
    if isinstance(labels, str):
        return [labels[:40]]
    result: List[str] = []
    for item in labels if isinstance(labels, (list, tuple)) else []:
        name = (
            str(item.get("name") or item.get("code") or "").strip()
            if isinstance(item, dict)
            else str(item).strip()
        )
        if name:
            safe_name = _redact_internal_text(name, maximum=40)
            if safe_name:
                result.append(safe_name)
        if len(result) >= 3:
            break
    return result


def _entry_date(ref: Mapping[str, Any]) -> str:
    for key in ("created_at", "entry_created_at", "occurred_at", "date"):
        if ref.get(key):
            return str(ref.get(key))[:32]
    return ""


def build_safe_summary(
    ref: Mapping[str, Any],
    *,
    alias: str,
    hybrid_rank: int,
) -> Dict[str, Any]:
    """Build the only candidate payload visible to the selector LLM."""
    evidence = ref.get("judge_evidence") if isinstance(ref.get("judge_evidence"), dict) else {}
    summary = {
        "alias": alias,
        "title": _safe_title(ref),
        "labels": _safe_labels(ref),
        "hit_window": _safe_hit_window(ref),
        "hybrid_rank": int(hybrid_rank),
        "title_term_hit": _safe_bool(evidence.get("title_term_hit")),
        "body_term_hit": _safe_bool(evidence.get("body_term_hit")),
        "term_hit_count": min(_safe_nonneg_int(evidence.get("term_hit_count")), 32),
        "evidence_coverage": _safe_coverage(evidence.get("evidence_coverage")),
    }
    metadata = ref.get("metadata") if isinstance(ref.get("metadata"), dict) else {}
    provider = str(metadata.get("provider") or "").strip().lower()
    if provider in {"notion", "growthlog", "future_connector"}:
        summary.update(
            {
                "source_provider": provider,
                "source_provider_label": _redact_internal_text(
                    metadata.get("provider_label"), maximum=40
                ),
                "source_object_kind": (
                    str(metadata.get("object_kind"))
                    if str(metadata.get("object_kind"))
                    in {"record", "todo", "page", "database_row", "attachment"}
                    else "record"
                ),
                "source_breadcrumb": _redact_internal_text(
                    metadata.get("breadcrumb"), maximum=240
                ),
                "source_sync_status": _redact_internal_text(
                    metadata.get("sync_status"), maximum=24
                ),
            }
        )
    if str(ref.get("source_type") or "").lower() == "todo":
        path_titles = metadata.get("path_titles")
        summary.update(
            {
                "source_kind": "todo",
                "task_path": [
                    safe
                    for safe in (
                        _redact_internal_text(item, maximum=80)
                        for item in (path_titles if isinstance(path_titles, list) else [])
                    )
                    if safe
                ][:4],
                "task_status": (
                    str(metadata.get("status"))
                    if str(metadata.get("status")) in {"open", "completed"}
                    else "unknown"
                ),
                "task_priority": (
                    str(metadata.get("priority"))
                    if str(metadata.get("priority")) in {"P0", "P1", "P2", "P3", "P4"}
                    else "P4"
                ),
                "task_urgent": _safe_bool(metadata.get("urgent")),
                "task_due_date": _redact_internal_text(
                    metadata.get("due_date"), maximum=32
                ),
                "task_completed_at": _redact_internal_text(
                    metadata.get("completed_at"), maximum=32
                ),
            }
        )
    return summary


def evidence_strength(ref: Mapping[str, Any]) -> Tuple[int, int, float]:
    """Compatibility diagnostic used only for same-level ordering."""
    evidence = ref.get("judge_evidence") if isinstance(ref.get("judge_evidence"), dict) else {}
    return (
        int(_safe_bool(evidence.get("title_term_hit"))),
        min(_safe_nonneg_int(evidence.get("term_hit_count")), 32),
        _safe_coverage(evidence.get("evidence_coverage")),
    )


def _sanitize_reason(value: Any) -> str:
    reason = re.sub(r"\s+", " ", str(value or "")).strip()
    if not reason or _INTERNAL_REASON_RE.search(reason):
        return "内容与检索主题相关"
    return reason[:80]


def deterministic_relevance_reason(ref: Mapping[str, Any]) -> str:
    """Build public copy from safe evidence without asking the LLM for prose."""
    evidence = ref.get("judge_evidence") if isinstance(ref.get("judge_evidence"), dict) else {}
    title_hit = _safe_bool(evidence.get("title_term_hit"))
    body_hit = _safe_bool(evidence.get("body_term_hit"))
    term_hits = _safe_nonneg_int(evidence.get("term_hit_count"))
    coverage = _safe_coverage(evidence.get("evidence_coverage"))
    if title_hit:
        return "标题直接命中检索主题"
    if body_hit and term_hits >= 2:
        return "正文多处涉及检索主题"
    if body_hit:
        return "正文包含相关内容"
    if coverage >= 0.5:
        return "内容较完整地覆盖检索主题"
    return "内容与检索主题相关"


class RetrieverSynthesisError(Exception):
    """Raised when a retriever answer violates the public citation contract."""

    def __init__(self, code: str, detail: str):
        self.code = str(code)
        self.detail = str(detail)
        super().__init__(self.detail)


def _strip_outer_code_fence(value: Any) -> str:
    text = str(value or "").strip()
    if not text.startswith("```") or not text.endswith("```"):
        return text
    lines = text.splitlines()
    if len(lines) >= 2 and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _normalize_alias_citations(value: str) -> str:
    return _COMMON_ALIAS_CITATION_RE.sub(lambda match: f"〔{match.group(1)}〕", value)


def _normalize_leading_alias_labels(
    value: str, allowed_aliases: Sequence[str]
) -> str:
    """Remove a common model-only bullet label when the alias is cited later."""
    normalized = value
    for alias in allowed_aliases:
        pattern = re.compile(
            rf"(?m)^(\s*[-*]\s*)(?:\*\*)?{re.escape(str(alias))}(?:\*\*)?\s*[：:]\s*"
        )
        normalized = pattern.sub(r"\1", normalized)
    return normalized


def _normalize_bare_allowed_aliases(
    value: str, allowed_aliases: Sequence[str]
) -> str:
    """Convert an allowed naked alias into a citation; unknown aliases still fail."""
    normalized = value
    for alias in allowed_aliases:
        pattern = re.compile(rf"(?<!〔)\b{re.escape(str(alias))}\b(?!〕)")
        normalized = pattern.sub(f"〔{alias}〕", normalized)
    return normalized


def validate_retriever_synthesis(
    value: Any,
    *,
    allowed_aliases: Sequence[str],
) -> Dict[str, Any]:
    """Validate model prose and convert opaque aliases to public citation numbers."""
    text = _strip_outer_code_fence(value)
    if not text:
        raise RetrieverSynthesisError("SYNTHESIS_EMPTY", "empty_output")
    if len(text) > MAX_SYNTHESIS_OUTPUT_CHARS:
        raise RetrieverSynthesisError("SYNTHESIS_TOO_LONG", "output_too_long")
    if _INTERNAL_REASON_RE.search(text):
        raise RetrieverSynthesisError("SYNTHESIS_INTERNAL_FIELD", "internal_field")
    if NO_RELEVANT_SOURCES in text:
        remainder = text.replace(NO_RELEVANT_SOURCES, "").strip()
        if _ANY_ALIAS_RE.search(remainder) or _ALIAS_CITATION_RE.search(remainder):
            raise RetrieverSynthesisError(
                "SYNTHESIS_SENTINEL_MIXED", "sentinel_with_reference"
            )
        return {"answer": "", "aliases": []}

    normalized = _normalize_alias_citations(text)
    normalized = _normalize_leading_alias_labels(normalized, allowed_aliases)
    normalized = _normalize_bare_allowed_aliases(normalized, allowed_aliases)
    allowed = set(str(alias) for alias in allowed_aliases)
    ordered_aliases: List[str] = []
    for alias in _ALIAS_CITATION_RE.findall(normalized):
        if alias not in allowed:
            raise RetrieverSynthesisError("SYNTHESIS_UNKNOWN_ALIAS", "unknown_alias")
        if alias not in ordered_aliases:
            ordered_aliases.append(alias)
    if not ordered_aliases:
        raise RetrieverSynthesisError("SYNTHESIS_CITATION_MISSING", "citation_missing")
    if len(ordered_aliases) > _selection_cap():
        raise RetrieverSynthesisError("SYNTHESIS_CITATION_LIMIT", "citation_limit")

    without_citations = _ALIAS_CITATION_RE.sub("", normalized)
    if _ANY_ALIAS_RE.search(without_citations):
        raise RetrieverSynthesisError("SYNTHESIS_ALIAS_FORMAT", "alias_not_cited")

    public_index = {
        alias: index for index, alias in enumerate(ordered_aliases, start=1)
    }
    public_answer = _ALIAS_CITATION_RE.sub(
        lambda match: f"〔{public_index[match.group(1)]}〕",
        normalized,
    ).strip()
    if not public_answer or _INTERNAL_REASON_RE.search(public_answer):
        raise RetrieverSynthesisError("SYNTHESIS_PUBLIC_INVALID", "public_invalid")
    return {"answer": public_answer, "aliases": ordered_aliases}


def build_synthesis_prompt(
    *,
    user_query: str,
    target_topic: str,
    presentation_mode: str,
    summaries: Sequence[Mapping[str, Any]],
) -> str:
    mode_instruction = {
        "find_one": "优先给出少量最相关来源；若有多个明显相关来源，可以一并列出。",
        "list_all": "在输出预算内尽可能覆盖全部实质相关来源，但排除仅偶然含词的记录。",
        "ordinary": "直接回应用户请求，并只保留能支撑回应的来源。",
    }.get(str(presentation_mode or ""), "直接回应用户请求。")
    serialized = json.dumps(list(summaries), ensure_ascii=False, separators=(",", ":"))
    return (
        f"user_request: {user_query}\n"
        f"target_topic: {target_topic}\n"
        f"presentation_mode: {presentation_mode}\n"
        f"presentation_instruction: {mode_instruction}\n"
        "候选安全摘要（按 Hybrid 相关度排序）：\n"
        f"{serialized}\n"
        "请直接输出带 〔S#〕 引用的最终中文答案。"
    )


def _budget_selection_summaries(
    summaries: Sequence[Mapping[str, Any]],
) -> Tuple[List[Dict[str, Any]], str]:
    """Keep Hybrid order while enforcing both count and serialized-char caps."""
    selected: List[Dict[str, Any]] = []
    payload = "[]"
    for raw in summaries[:_selection_input_cap()]:
        candidate = [*selected, dict(raw)]
        serialized = json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
        if len(serialized) > _selection_input_char_budget():
            break
        selected = candidate
        payload = serialized
    return selected, payload


async def _synthesize_once(
    *,
    user_query: str,
    target_topic: str,
    presentation_mode: str,
    summaries: Sequence[Mapping[str, Any]],
    model_key: Optional[str],
    user_id: Optional[int],
    fallback_candidates: Optional[List[str]],
    system_policy: Optional[str],
) -> Tuple[Dict[str, Any], str, Optional[str]]:
    aliases = [str(item["alias"]) for item in summaries]
    prompt = build_synthesis_prompt(
        user_query=user_query,
        target_topic=target_topic,
        presentation_mode=presentation_mode,
        summaries=summaries,
    )
    system = SYNTHESIS_SYSTEM
    if system_policy:
        system = f"{system}\n\n{system_policy}"
    result = await generate_chat_completion(
        system=system,
        messages=[{"role": "user", "content": prompt}],
        mode="retrieval",
        model_key=model_key,
        user_id=user_id,
        fallback_candidates=fallback_candidates,
        max_tokens=2600,
        temperature=0.2,
    )
    try:
        parsed = validate_retriever_synthesis(
            result.content,
            allowed_aliases=aliases,
        )
    except RetrieverSynthesisError as exc:
        exc.invalid_output = result.content
        exc.original_prompt = prompt
        raise
    return parsed, prompt, result.fallback_reason


async def _repair_synthesis_once(
    *,
    original_prompt: str,
    invalid_output: str,
    allowed_aliases: Sequence[str],
    model_key: Optional[str],
    user_id: Optional[int],
    fallback_candidates: Optional[List[str]],
) -> Dict[str, Any]:
    repair_prompt = (
        f"原任务与候选：\n{original_prompt}\n\n"
        f"允许的 alias：{', '.join(allowed_aliases)}\n"
        f"待修复答案：\n{invalid_output}\n\n"
        "只输出修复后的最终中文答案。"
    )
    result = await generate_chat_completion(
        system=CITATION_REPAIR_SYSTEM,
        messages=[{"role": "user", "content": repair_prompt}],
        mode="retrieval",
        model_key=model_key,
        user_id=user_id,
        fallback_candidates=fallback_candidates,
        max_tokens=2600,
        temperature=0.0,
    )
    return validate_retriever_synthesis(
        result.content,
        allowed_aliases=allowed_aliases,
    )


async def rank_candidates_by_relevance(
    *,
    query: str,
    candidates: Sequence[Mapping[str, Any]],
    topic_terms: Sequence[str],
    user_query: Optional[str] = None,
    presentation_mode: str = "ordinary",
    model_key: Optional[str] = None,
    user_id: Optional[int] = None,
    fallback_candidates: Optional[List[str]] = None,
    system_policy: Optional[str] = None,
) -> Dict[str, Any]:
    """Use one bounded LLM turn to select, summarize, and cite RAG candidates."""
    annotated: List[Dict[str, Any]] = []
    alias_to_ref: Dict[str, Dict[str, Any]] = {}
    summaries: List[Dict[str, Any]] = []
    for index, raw in enumerate(candidates):
        ref = dict(raw)
        alias = f"S{index + 1}"
        ref["hybrid_rank"] = index
        ref["relevance_alias"] = alias
        ref["relevance_level"] = LEVEL_IRRELEVANT
        ref["relevance_reason"] = ""
        ref["_candidate_index"] = index
        annotated.append(ref)
        alias_to_ref[alias] = ref
        summaries.append(build_safe_summary(ref, alias=alias, hybrid_rank=index))

    pool_keys = [
        str(ref.get("reference_key") or "")
        for ref in annotated
        if str(ref.get("reference_key") or "").strip()
    ]
    empty_meta = {
        "strategy": "single_llm_semantic_synthesis",
        "degraded": False,
        "ranking_degraded": False,
        "selection_provider_calls": 0,
        "synthesis_provider_calls": 0,
        "selection_input_count": 0,
        "selection_payload_chars": 0,
        "selection_repaired": False,
        "synthesis_repaired": False,
        "synthesis_output_chars": 0,
        "selection_truncated": False,
        "selection_unreviewed_count": 0,
        "rag_admission_input_count": 0,
        "rag_admission_count": 0,
        "rag_admission_rejected_count": 0,
        "rag_admission_channel_input": {},
        "rag_admission_channel_admitted": {},
        "rag_admission_channel_thresholds": {},
        "rag_admission_floor_added_count": 0,
        "rag_admission_global_floor_added_count": 0,
        "llm_scored_count": 0,
        "fallback_count": 0,
        "unresolved_count": 0,
        "scoring_origin": {},
        "provider_error_code": None,
        "ranked_count": 0,
        "relevant_count": 0,
        "relevance_threshold": RELEVANCE_THRESHOLD,
    }
    if not annotated:
        return {
            "ok": True,
            "found": False,
            "answer": "",
            "ranked": [],
            "retrieval_results": [],
            "answer_refs": [],
            "candidate_pool_fingerprint": candidate_pool_fingerprint([]),
            "ranking_fingerprint": ranking_fingerprint([]),
            "topic_fingerprint": retrieval_topic_fingerprint(topic_terms),
            "meta": empty_meta,
        }

    admission_summaries, admission_meta = select_rag_admission_summaries(
        annotated, summaries
    )
    selection_summaries, selection_payload = _budget_selection_summaries(
        admission_summaries
    )
    allowed_aliases = [str(item["alias"]) for item in selection_summaries]
    degraded = False
    fallback_count = 0
    selection_repaired = False
    selection_provider_calls = 0
    provider_error_code: Optional[str] = None
    provider_fallback_used = False
    answer_text = ""
    selected_aliases: List[str] = []

    try:
        parsed, _original_prompt, provider_fallback_reason = await _synthesize_once(
            user_query=(user_query or query),
            target_topic=query,
            presentation_mode=presentation_mode,
            summaries=selection_summaries,
            model_key=model_key,
            user_id=user_id,
            fallback_candidates=fallback_candidates,
            system_policy=system_policy,
        )
        selection_provider_calls = 1
        provider_fallback_used = bool(provider_fallback_reason)
        answer_text = str(parsed.get("answer") or "")
        selected_aliases = [str(alias) for alias in parsed.get("aliases") or []]
    except RetrieverSynthesisError as exc:
        selection_provider_calls = 1
        try:
            repaired = await _repair_synthesis_once(
                original_prompt=str(getattr(exc, "original_prompt", "")),
                invalid_output=str(getattr(exc, "invalid_output", "")),
                allowed_aliases=allowed_aliases,
                model_key=model_key,
                user_id=user_id,
                fallback_candidates=fallback_candidates,
            )
            selection_provider_calls = 2
            selection_repaired = True
            answer_text = str(repaired.get("answer") or "")
            selected_aliases = [str(alias) for alias in repaired.get("aliases") or []]
        except (RetrieverSynthesisError, LLMGatewayError) as repair_exc:
            degraded = True
            selection_provider_calls = 2
            provider_error_code = str(getattr(repair_exc, "code", "SYNTHESIS_REPAIR_FAILED"))
    except LLMGatewayError as exc:
        degraded = True
        selection_provider_calls = 1
        provider_error_code = str(exc.code)

    if degraded:
        limit = min(_fallback_cap(), len(selection_summaries))
        selected_aliases = allowed_aliases[:limit]
        answer_text = ""
        fallback_count = limit
        unresolved_count = len(selection_summaries)
        llm_scored_count = 0
        origin_counts = {
            "hybrid_fallback": limit,
            "rag_threshold_rejected": int(admission_meta["rejected_count"]),
            "admitted_fallback_omitted": max(0, len(admission_summaries) - limit),
        }
        logger.warning(
            "retriever semantic synthesis degraded code=%s candidate_count=%s selection_count=%s",
            provider_error_code,
            len(annotated),
            len(selection_summaries),
        )
    else:
        unresolved_count = 0
        llm_scored_count = len(selection_summaries)
        origin_counts = {
            "llm_semantic_synthesis": len(selected_aliases),
            "llm_omitted": max(0, len(selection_summaries) - len(selected_aliases)),
            "rag_threshold_rejected": int(admission_meta["rejected_count"]),
            "budget_unreviewed": max(
                0, len(admission_summaries) - len(selection_summaries)
            ),
        }

    selected_refs: List[Dict[str, Any]] = []
    selected_alias_set = set(selected_aliases)
    for alias in selected_aliases:
        ref = alias_to_ref.get(alias)
        if ref is None:
            continue
        ref["relevance_level"] = LEVEL_USEFUL if degraded else LEVEL_STRONG
        ref["relevance_reason"] = (
            deterministic_relevance_reason(ref)
            if degraded
            else "AI 已确认内容与检索主题相关"
        )
        ref["scoring_origin"] = (
            "hybrid_fallback" if degraded else "llm_semantic_synthesis"
        )
        selected_refs.append(ref)

    ranked = selected_refs + [
        ref
        for ref in annotated
        if str(ref.get("relevance_alias") or "") not in selected_alias_set
    ]
    rank_keys = [
        str(ref.get("reference_key") or "")
        for ref in selected_refs
        if str(ref.get("reference_key") or "").strip()
    ]

    return {
        "ok": True,
        "found": bool(selected_refs),
        "answer": answer_text,
        "ranked": ranked,
        "retrieval_results": selected_refs,
        "answer_refs": list(selected_refs[:_selection_cap()]),
        "candidate_pool_fingerprint": candidate_pool_fingerprint(pool_keys),
        "ranking_fingerprint": ranking_fingerprint(rank_keys),
        "topic_fingerprint": retrieval_topic_fingerprint(topic_terms),
        "meta": {
            "strategy": "single_llm_semantic_synthesis",
            "degraded": degraded,
            "ranking_degraded": degraded,
            "selection_provider_calls": selection_provider_calls,
            "synthesis_provider_calls": selection_provider_calls,
            "selection_input_count": len(selection_summaries),
            "selection_payload_chars": len(selection_payload),
            "selection_repaired": selection_repaired,
            "synthesis_repaired": selection_repaired,
            "synthesis_output_chars": len(answer_text),
            "selection_truncated": len(selection_summaries) < len(admission_summaries),
            "selection_unreviewed_count": max(
                0, len(admission_summaries) - len(selection_summaries)
            ),
            "rag_admission_input_count": int(admission_meta["input_count"]),
            "rag_admission_count": int(admission_meta["admitted_count"]),
            "rag_admission_rejected_count": int(admission_meta["rejected_count"]),
            "rag_admission_channel_input": dict(admission_meta["channel_input"]),
            "rag_admission_channel_admitted": dict(
                admission_meta["channel_admitted"]
            ),
            "rag_admission_channel_thresholds": dict(
                admission_meta["channel_thresholds"]
            ),
            "rag_admission_floor_added_count": int(
                admission_meta["floor_added_count"]
            ),
            "rag_admission_global_floor_added_count": int(
                admission_meta["global_floor_added_count"]
            ),
            "llm_scored_count": llm_scored_count,
            "fallback_count": fallback_count,
            "unresolved_count": unresolved_count,
            "scoring_origin": origin_counts,
            "provider_error_code": provider_error_code,
            "provider_fallback_used": provider_fallback_used,
            "ranked_count": len(ranked),
            "relevant_count": len(selected_refs),
            "relevance_threshold": RELEVANCE_THRESHOLD,
        },
    }
