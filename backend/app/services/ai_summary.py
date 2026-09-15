"""
AI 总结服务
通过 llm_gateway（DeepSeek）生成 RAG 回答
两次调用流程：1) 判断相关性 2) 生成回答

Phase C.1: 相关性判定按 reference_key（entry / attachment_chunk）独立筛选，
避免同 entry_id 下的附件 chunk 被 entry 判定误杀。
Phase C.1.1: judge/answer 均走 llm_gateway，不再直接调用 MiniMax。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from app.services.llm_gateway import (
    DEFAULT_MODEL_KEY,
    LLMGatewayError,
    LLMGatewayResult,
    generate_chat_completion,
)
from app.services.context_builder import build_source_aware_context
from app.services.evidence_text import evidence_blocks_for_prompt
from app.services.reference_identity import (
    annotate_with_canonical_keys,
    canonical_reference_key,
    dedupe_by_evidence_origin,
    normalize_judge_key_list,
)

logger = logging.getLogger(__name__)

# Relevance classification must be deterministic; answer generation may stay warmer.
RELEVANCE_JUDGE_TEMPERATURE = 0.0
ANSWER_GENERATION_TEMPERATURE = 0.3
RELEVANCE_VALID_THRESHOLD = 2

# Production default: deterministic context + structured used citations.
# hard_judge retained for A/B comparison only.
CONTEXT_STRATEGY_DETERMINISTIC = "deterministic"
CONTEXT_STRATEGY_HARD_JUDGE = "hard_judge"
CONTEXT_STRATEGY_SOFT_JUDGE = "soft_judge"
DEFAULT_CONTEXT_STRATEGY = CONTEXT_STRATEGY_DETERMINISTIC

ABSTENTION_ANSWER = (
    "在您的记录中没有找到与这个问题相关的内容。您可以换个关键词试试，或者先记录一些相关内容。"
)
_ABSTENTION_RE = re.compile(
    r"(在您的记录中没有找到|没有找到与这个问题相关|没有与这个问题相关的内容|"
    r"未找到与.*相关|没有找到相关内容)"
)
_WEAK_ABSTENTION_RE = re.compile(
    r"(没有找到|未找到|资料不足|证据不足|无法确认|缺少.*信息|暂时不能判断)"
)


def re_search_abstention(answer: str) -> bool:
    """Match genuine refusals; do not treat mid-answer hedges as abstention."""
    text = (answer or "").strip()
    if not text:
        return True
    if _ABSTENTION_RE.search(text):
        return True
    if _WEAK_ABSTENTION_RE.search(text) and len(text) <= 40:
        return True
    return False

# 第一次调用：判断相关性的 System Prompt
JUDGE_SYSTEM_PROMPT = """你是一个知识检索助手。请**独立**判断每条参考内容对用户问题的相关等级。

相关等级（relevance，整数）：
- 0：完全无关
- 1：主题相近但不支持作答
- 2：直接支持回答问题的部分要点
- 3：关键直接证据（事实/时间/对象对得上）

判断标准：
- 逐条独立判定，互不影响
- 只有内容明确支持用户问题才给 2 或 3；拿不准给 0 或 1
- 仅关键词重合或背景相关 → 最多 1
- 多证据问题：对每条必要证据分别打分，不要默认只留最相似一条
- entry / attachment_chunk / knowledge_source / todo 分别判断
- 只能使用给定的 reference_key；禁止编造；禁止重复

输出 JSON（不要输出其他内容）：
{
  "candidates": [
    {"reference_key": "给定的key", "relevance": 0}
  ],
  "valid_reference_keys": ["兼容字段：relevance>=2 的 key 列表，可空"],
  "invalid_reference_keys": ["relevance<=1 的 key 列表，可空"],
  "reasoning": "1-2句，勿复述隐私正文"
}"""


ANSWER_SYSTEM_PROMPT = """你是 GrowthLog 的个人助手。请基于参考内容回答用户问题，并声明实际使用的证据。

要求：
- 先直接给出结论，再补充简短依据；可执行建议仅在有证据时给出
- 只基于提供的参考内容回答，不要编造信息
- 拒答条件极严：仅当全部参考都与问题无关时，才可写“在您的记录中没有找到相关内容”，且 used_reference_keys=[]、claims=[]
- 只要任一参考（含短句/标题式记录）能支撑问题中的事实，就必须回答并产出 claims，禁止拒答
- 不要因为参考很短、很简略或夹在多条参考中间就忽略；请逐条阅读后再决定
- 不要使用检索报告腔
- answer 正文面向普通用户，禁止输出 entry_id、chunk_id、source_type、reference_key、字段名或数据库结构
- used_reference_keys 只能从给定允许列表中选择，且必须是回答真正用到的证据；未使用的不要列入

关于 claims（必须遵守）：
- 每条 claim 必须是最小、原子化、可独立核验的事实（一个 claim 只表达一个事实）
- 不得把时间先后推断成因果；不得把局部记录概括成长期/全局结论
- 没有明确证据的修饰、原因、程度、结论应删除，或明确写成不确定；不确定时仍应回答已有证据支持的部分，而不是整题拒答
- 每条 claim 绑定“最小充分引用集”：只绑真正支撑该事实的 key；不要为了展示更多引用而绑无关来源
- 仅当多条记录共同才足以支持时，才允许一条 claim 绑定多个 key
- claim.reference_keys 必须是 used_reference_keys 的子集

必须只输出 JSON（不要其它文字）：
{
  "answer": "自然语言回答",
  "used_reference_keys": ["允许列表中的 key"],
  "claims": [{"text": "原子化事实陈述", "reference_keys": ["key"]}]
}"""

RETRIEVER_ANSWER_SYSTEM_PROMPT = """你是 GrowthLog 检索员。目标是快速告诉用户找到了哪些记录，不要写成持续讲解。

要求：
- 回答简洁：先列出找到的记录要点，再给一句总结
- 只基于参考内容，不要编造
- 禁止输出 entry_id、chunk_id、source_type、reference_key、字段名或数据库结构
- used_reference_keys 只能从允许列表选择

必须只输出 JSON：
{
  "answer": "简洁自然语言回答",
  "used_reference_keys": ["允许列表中的 key"],
  "claims": [{"text": "原子化事实陈述", "reference_keys": ["key"]}]
}"""


def _compose_system_prompt(
    base: str,
    *,
    system_policy: Optional[str] = None,
) -> str:
    policy = (system_policy or "").strip()
    if not policy:
        return base
    return f"{policy}\n\n{base}"


def _answer_system_for_style(answer_style: str) -> str:
    style = (answer_style or "default").strip().lower()
    if style == "retriever":
        return RETRIEVER_ANSWER_SYSTEM_PROMPT
    return ANSWER_SYSTEM_PROMPT


def _created_at_prefix(value: Any, n: int = 10) -> str:
    """Normalize created_at (str or datetime) for prompt display."""
    if value is None or value == "":
        return ""
    return str(value)[:n]


def build_reference_key(ref: Dict[str, Any], index: int = 0) -> str:
    """Canonical reference_key (result_key form). Legacy short keys are aliases only."""
    return canonical_reference_key(ref, index)


def _attachment_filename(ref: Dict[str, Any]) -> str:
    metadata = ref.get("metadata") if isinstance(ref.get("metadata"), dict) else {}
    filename = metadata.get("filename")
    if isinstance(filename, str) and filename.strip():
        return filename.strip()
    title = str(ref.get("title") or "")
    return title.split("（", 1)[0].strip()


def annotate_references_with_keys(references: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return shallow copies with canonical reference_key attached."""
    return annotate_with_canonical_keys(references)


def _coerce_int_ids(values: Any) -> Set[int]:
    result: Set[int] = set()
    if not isinstance(values, list):
        return result
    for value in values:
        try:
            result.add(int(value))
        except (TypeError, ValueError):
            continue
    return result


def _coerce_str_keys(values: Any) -> Set[str]:
    result: Set[str] = set()
    if not isinstance(values, list):
        return result
    for value in values:
        if isinstance(value, str) and value.strip():
            result.add(value.strip())
    return result


def select_refs_by_judge_result(
    references: List[Dict[str, Any]],
    judge_result: Dict[str, Any],
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split references into valid/invalid using scored candidates or legacy keys.

    New path: candidates[{reference_key, relevance}]; relevance>=2 → valid.
    Legacy: valid_reference_keys / valid_entry_ids (entry-only for entry_ids).
    """
    annotated = annotate_references_with_keys(references)
    known_keys = {ref["reference_key"] for ref in annotated}
    by_key = {ref["reference_key"]: ref for ref in annotated}

    score_map: Dict[str, int] = {}
    unknown_valid: List[str] = []
    dup_valid: List[str] = []
    for row in judge_result.get("candidates") or []:
        if not isinstance(row, dict):
            continue
        raw_key = row.get("reference_key")
        if not isinstance(raw_key, str) or not raw_key.strip():
            continue
        from app.services.reference_identity import resolve_key_against_known

        resolved = resolve_key_against_known(raw_key.strip(), known_keys)
        if resolved is None:
            unknown_valid.append(raw_key.strip())
            continue
        try:
            rel = int(row.get("relevance"))
        except (TypeError, ValueError):
            continue
        if resolved in score_map:
            dup_valid.append(raw_key.strip())
            score_map[resolved] = max(score_map[resolved], rel)
        else:
            score_map[resolved] = rel

    valid_list: List[str] = []
    if score_map:
        valid_list = [
            key for key, rel in score_map.items() if rel >= RELEVANCE_VALID_THRESHOLD
        ]
    else:
        valid_list, unknown_valid, dup_valid = normalize_judge_key_list(
            list(judge_result.get("valid_reference_keys") or []),
            known_keys,
        )

    invalid_list, _unknown_invalid, _dup_invalid = normalize_judge_key_list(
        list(judge_result.get("invalid_reference_keys") or []),
        known_keys,
    )
    valid_keys = set(valid_list)
    invalid_keys = set(invalid_list)
    judge_result = dict(judge_result)
    judge_result["_rejected_unknown_valid_keys"] = unknown_valid
    judge_result["_rejected_duplicate_valid_keys"] = dup_valid
    judge_result["_relevance_scores"] = score_map

    legacy_valid_entry_ids = _coerce_int_ids(judge_result.get("valid_entry_ids"))
    legacy_invalid_entry_ids = _coerce_int_ids(judge_result.get("invalid_entry_ids"))
    use_legacy_entry_ids = (
        not score_map
        and not valid_keys
        and not invalid_keys
        and (bool(legacy_valid_entry_ids) or bool(legacy_invalid_entry_ids))
    )

    valid_refs: List[Dict[str, Any]] = []
    invalid_refs: List[Dict[str, Any]] = []

    for ref in annotated:
        key = ref["reference_key"]
        source_type = ref.get("source_type") or "entry"
        entry_id = ref.get("entry_id")
        if score_map:
            rel = score_map.get(key)
            if rel is not None and rel >= RELEVANCE_VALID_THRESHOLD:
                item = dict(ref)
                item["judge_relevance"] = rel
                valid_refs.append(item)
            else:
                invalid_refs.append({
                    "entry_id": entry_id,
                    "reference_key": key,
                    "source_type": source_type,
                    "judge_relevance": rel,
                    "reason": (
                        f"relevance={rel}<{RELEVANCE_VALID_THRESHOLD}"
                        if rel is not None
                        else "未进入 scored candidates"
                    ),
                })
            continue

        if valid_keys or invalid_keys:
            if key in valid_keys:
                valid_refs.append(ref)
            else:
                reason = (
                    "第一次 LLM 判断为不相关"
                    if key in invalid_keys
                    else "未进入 valid_reference_keys"
                )
                invalid_refs.append({
                    "entry_id": entry_id,
                    "reference_key": key,
                    "source_type": source_type,
                    "reason": reason,
                })
            continue

        if use_legacy_entry_ids:
            if source_type in {"attachment_chunk", "knowledge_source", "todo"}:
                invalid_refs.append({
                    "entry_id": entry_id,
                    "reference_key": key,
                    "source_type": source_type,
                    "reason": "旧版 valid_entry_ids 不适用于附件/knowledge chunk",
                })
                continue
            if entry_id in legacy_valid_entry_ids:
                valid_refs.append(ref)
            else:
                invalid_refs.append({
                    "entry_id": entry_id,
                    "reference_key": key,
                    "source_type": source_type,
                    "reason": (
                        "第一次 LLM 判断为不相关"
                        if entry_id in legacy_invalid_entry_ids
                        else "未进入 valid_entry_ids"
                    ),
                })
            continue

        invalid_refs.append({
            "entry_id": entry_id,
            "reference_key": key,
            "source_type": source_type,
            "reason": "缺少可用的相关性判定结果",
        })

    if valid_list:
        order = {key: idx for idx, key in enumerate(valid_list)}
        valid_refs.sort(key=lambda item: order.get(item["reference_key"], 10_000))

    # Cross-source mirror dedupe on valid set (prefer attachment_chunk).
    valid_refs, mirror_dropped = dedupe_by_evidence_origin(valid_refs)
    for drop in mirror_dropped:
        invalid_refs.append({
            "entry_id": drop.get("entry_id"),
            "reference_key": drop.get("reference_key"),
            "source_type": drop.get("source_type"),
            "evidence_origin_key": drop.get("evidence_origin_key"),
            "reason": "duplicate_evidence_mirror",
        })

    # Keep only keys that exist in the candidate pool.
    valid_refs = [ref for ref in valid_refs if ref.get("reference_key") in by_key]
    return valid_refs[:10], invalid_refs


def format_valid_reference(ref: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a valid reference for API / frontend cards."""
    metadata = ref.get("metadata") if isinstance(ref.get("metadata"), dict) else {}
    metadata = dict(metadata)
    key = canonical_reference_key(ref)
    # Derive API source_type from canonical key prefix (stable for clients).
    source_type = str(ref.get("source_type") or "entry").strip() or "entry"
    if key.startswith("attachment_chunk:"):
        source_type = "attachment_chunk"
    elif key.startswith("knowledge_source:"):
        source_type = "knowledge_source"
    elif key.startswith("entry:"):
        source_type = "entry"
    elif key.startswith("todo:"):
        source_type = "todo"
    if not metadata.get("filename"):
        filename = _attachment_filename(ref)
        if filename and source_type in {"attachment_chunk", "knowledge_source"}:
            metadata["filename"] = filename

    return {
        "entry_id": ref.get("entry_id"),
        "title": ref.get("title", ""),
        "label_name": ref.get("label_name", ""),
        "created_at": ref.get("created_at", ""),
        "snippet": ref.get("snippet", ""),
        "relevance_score": ref.get("relevance_score", 0.0),
        "source_type": source_type,
        "attachment_id": ref.get("attachment_id"),
        "chunk_id": ref.get("chunk_id"),
        "page_no": ref.get("page_no"),
        "slide_no": ref.get("slide_no"),
        "modality": ref.get("modality"),
        "retrieval_method": ref.get("retrieval_method"),
        "retrieval_sources": ref.get("retrieval_sources"),
        "metadata": metadata,
        "source_id": ref.get("source_id"),
        "source_reason": ref.get("source_reason"),
        "chunk_index": ref.get("chunk_index"),
        "evidence_origin_key": ref.get("evidence_origin_key"),
        # Stable citation identity for clients; must not appear in answer body.
        "reference_key": key,
    }


def _parse_judge_response(raw_response: str) -> Dict[str, Any]:
    """Parse first-pass LLM JSON response (reference_key or legacy entry_id).

    JSON/parse failures raise LLMGatewayError so callers do not treat them as
    business-level "not relevant".
    """
    if not isinstance(raw_response, str) or not raw_response.strip():
        raise LLMGatewayError("相关性判定结果为空", code="JUDGE_PARSE_FAILED")

    try:
        text = raw_response
        if "```json" in text:
            start = text.find("```json") + 7
            end = text.find("```", start)
            if end > start:
                text = text[start:end].strip()
        elif "```" in text:
            start = text.find("```") + 3
            end = text.find("```", start)
            if end > start:
                text = text[start:end].strip()

        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise LLMGatewayError("相关性判定结果格式无效", code="JUDGE_PARSE_FAILED")
        candidates = parsed.get("candidates")
        if not isinstance(candidates, list):
            candidates = []
        valid_keys = parsed.get("valid_reference_keys", [])
        # Derive valid_reference_keys from scored candidates when omitted.
        if not valid_keys and candidates:
            derived = []
            for row in candidates:
                if not isinstance(row, dict):
                    continue
                try:
                    rel = int(row.get("relevance"))
                except (TypeError, ValueError):
                    continue
                key = row.get("reference_key")
                if rel >= RELEVANCE_VALID_THRESHOLD and isinstance(key, str) and key.strip():
                    derived.append(key.strip())
            valid_keys = derived
        return {
            "candidates": candidates,
            "valid_reference_keys": valid_keys,
            "invalid_reference_keys": parsed.get("invalid_reference_keys", []),
            "valid_entry_ids": parsed.get("valid_entry_ids", []),
            "invalid_entry_ids": parsed.get("invalid_entry_ids", []),
            "reasoning": parsed.get("reasoning", ""),
        }
    except LLMGatewayError:
        raise
    except (json.JSONDecodeError, Exception) as exc:
        logger.error("JSON 解析失败: %s, 原始响应长度=%s", type(exc).__name__, len(raw_response or ""))
        raise LLMGatewayError("相关性判定结果无法解析", code="JUDGE_PARSE_FAILED") from exc


def _model_fallback_from_result(llm: LLMGatewayResult) -> Optional[Dict[str, str]]:
    if not llm.fallback_reason:
        return None
    return {
        "requested_model_key": llm.requested_model_key,
        "used_model_key": llm.used_model_key,
        "fallback_reason": llm.fallback_reason,
    }


async def _judge_relevance(
    query: str,
    references: List[Dict[str, Any]],
    conversation_history: Optional[List[Dict]] = None,
    model_key: Optional[str] = None,
    user_id: Optional[int] = None,
    fallback_candidates: Optional[List[str]] = None,
    *,
    system_policy: Optional[str] = None,
) -> Tuple[Dict[str, Any], Optional[Dict[str, str]]]:
    """First LLM call via gateway: judge relevance per reference_key."""
    annotated = annotate_references_with_keys(references)
    context_parts = []
    for ref in annotated:
        key = ref["reference_key"]
        snippet = str(ref.get("snippet") or ref.get("content") or "")[:500]
        title = str(ref.get("title") or "")
        created_at = _created_at_prefix(ref.get("created_at"))
        source_type = str(ref.get("source_type") or "entry")
        entry_id = ref.get("entry_id")
        if source_type == "todo":
            meta = ref.get("metadata") if isinstance(ref.get("metadata"), dict) else {}
            path = " > ".join(str(item) for item in (meta.get("path_titles") or []) if str(item).strip())
            context_parts.append(
                f"【reference_key={key}，source_type=todo】"
                f"小要事路径: {path or title} (创建时间: {created_at})\n{snippet}"
            )
        elif source_type in {"attachment_chunk", "attachment"}:
            filename = _attachment_filename(ref) or title
            location = ""
            if ref.get("page_no"):
                location = f"，第 {ref.get('page_no')} 页"
            elif ref.get("slide_no"):
                location = f"，第 {ref.get('slide_no')} 张幻灯片"
            context_parts.append(
                "【reference_key={key}，source_type=attachment，"
                "attachment_id={attachment_id}{location}】"
                "文件名: {filename} (时间: {created_at})\n{snippet}".format(
                    key=key,
                    attachment_id=ref.get("attachment_id") or ref.get("source_id"),
                    location=location,
                    filename=filename,
                    created_at=created_at,
                    snippet=snippet,
                )
            )
        else:
            context_parts.append(
                f"【reference_key={key}，source_type=entry，entry_id={entry_id}】"
                f"标题: {title} (时间: {created_at})\n{snippet}"
            )

    context = "\n\n".join(context_parts)
    allowed_keys = [ref["reference_key"] for ref in annotated]

    messages: List[Dict] = []
    if conversation_history:
        for msg in conversation_history[:-1]:
            role = msg["role"] if msg["role"] in ("user", "assistant") else "user"
            messages.append({"role": role, "content": msg["content"]})

    user_prompt = f"""参考内容（只能使用下列 reference_key；请逐条独立判断，可多选）：
{context}

允许的 reference_key 列表: {json.dumps(allowed_keys, ensure_ascii=False)}

用户问题: {query}

请对每条候选输出 reference_key + relevance(0-3)。relevance>=2 视为有效；无相关则 candidates 全为低分且 valid_reference_keys=[]:"""
    messages.append({"role": "user", "content": user_prompt})

    try:
        llm = await generate_chat_completion(
            model_key=model_key or DEFAULT_MODEL_KEY,
            system=_compose_system_prompt(JUDGE_SYSTEM_PROMPT, system_policy=system_policy),
            messages=messages,
            mode="retrieval",
            max_tokens=2000,
            temperature=RELEVANCE_JUDGE_TEMPERATURE,
            user_id=user_id,
            fallback_candidates=fallback_candidates,
        )
        raw_response = llm.content or ""
        # Do not log response body (may contain reference_key / snippets).
        logger.info(
            "Judge LLM response received length=%s used_model=%s",
            len(raw_response),
            llm.used_model_key,
        )
        return _parse_judge_response(raw_response), _model_fallback_from_result(llm)
    except LLMGatewayError:
        # Provider/config/parse failures must propagate; never mark all refs invalid.
        raise
    except Exception as exc:
        logger.error(
            "第一次 LLM 调用失败: code=%s error=%s",
            "LLM_PROVIDER_FAILED",
            type(exc).__name__ + ": " + str(exc)[:200],
        )
        raise LLMGatewayError("AI 服务暂时不可用", code="LLM_PROVIDER_FAILED") from exc


def _parse_structured_answer(
    raw_response: str,
    *,
    known_context_keys: Set[str],
) -> Dict[str, Any]:
    """Parse structured answer JSON; reject unknown keys; dedupe used keys."""
    if not isinstance(raw_response, str) or not raw_response.strip():
        raise LLMGatewayError("回答结果为空", code="ANSWER_PARSE_FAILED")
    text = raw_response.strip()
    try:
        if "```json" in text:
            start = text.find("```json") + 7
            end = text.find("```", start)
            if end > start:
                text = text[start:end].strip()
        elif "```" in text:
            start = text.find("```") + 3
            end = text.find("```", start)
            if end > start:
                text = text[start:end].strip()
        # Fallback: plain prose answer (legacy/compat)
        if not text.lstrip().startswith("{"):
            return {
                "answer": raw_response.strip(),
                "used_reference_keys": [],
                "claims": [],
                "_legacy_plain_answer": True,
            }
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise LLMGatewayError("回答结果格式无效", code="ANSWER_PARSE_FAILED")
    except LLMGatewayError:
        raise
    except Exception as exc:
        raise LLMGatewayError("回答结果无法解析", code="ANSWER_PARSE_FAILED") from exc

    answer = str(parsed.get("answer") or "").strip()
    used_list, unknown, duplicates = normalize_judge_key_list(
        list(parsed.get("used_reference_keys") or []),
        known_context_keys,
    )
    claims_out: List[Dict[str, Any]] = []
    for row in parsed.get("claims") or []:
        if not isinstance(row, dict):
            continue
        claim_text = str(row.get("text") or "").strip()
        claim_keys, _, _ = normalize_judge_key_list(
            list(row.get("reference_keys") or []),
            set(used_list) or known_context_keys,
        )
        # Claim keys must be subset of used keys when used is non-empty.
        if used_list:
            claim_keys = [k for k in claim_keys if k in set(used_list)]
        if claim_text:
            claims_out.append({"text": claim_text, "reference_keys": claim_keys})
    return {
        "answer": answer,
        "used_reference_keys": used_list,
        "claims": claims_out,
        "_rejected_unknown_used_keys": unknown,
        "_rejected_duplicate_used_keys": duplicates,
    }


def _evaluate_claim_contract(
    *,
    answer_text: str,
    used_keys: List[str],
    claims: List[Dict[str, Any]],
    known_keys: Set[str],
) -> Tuple[bool, List[str], List[str], List[Dict[str, Any]]]:
    """
    Validate answer citation contract.

    Returns: (invalid, reasons, normalized_used_keys, normalized_claims)
    Does NOT invent citations from candidates.
    """
    looks_abstention = re_search_abstention(answer_text)
    reasons: List[str] = []
    claims_out: List[Dict[str, Any]] = []
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        ckeys = [
            str(k).strip()
            for k in (claim.get("reference_keys") or [])
            if str(k).strip() and str(k).strip() in known_keys
        ]
        text = str(claim.get("text") or "").strip()
        if text:
            claims_out.append({"text": text, "reference_keys": ckeys})

    if looks_abstention or not (answer_text or "").strip():
        return False, [], list(used_keys), claims_out

    used_norm = [k for k in used_keys if k in known_keys]
    if not claims_out:
        reasons.append("claims_empty")
    if not used_norm:
        reasons.append("used_reference_keys_empty")

    used_set = set(used_norm)
    for claim in claims_out:
        ckeys = list(claim.get("reference_keys") or [])
        if str(claim.get("text") or "").strip() and not ckeys:
            reasons.append("claim_missing_keys")
        bad = [k for k in ckeys if k not in used_set and k not in known_keys]
        if bad:
            reasons.append("claim_keys_not_allowed")
        # Claim keys must be subset of used when used is non-empty.
        if used_set:
            kept = [k for k in ckeys if k in used_set]
            claim["reference_keys"] = kept
            if ckeys and not kept:
                reasons.append("claim_keys_not_subset_of_used")

    bound_keys = {
        str(k).strip()
        for claim in claims_out
        for k in (claim.get("reference_keys") or [])
        if str(k).strip()
    }
    unbound = [k for k in used_norm if k not in bound_keys]
    if unbound:
        reasons.append("used_key_not_bound_by_claim")
        used_norm = [k for k in used_norm if k in bound_keys]
    if claims_out and not used_norm:
        reasons.append("no_bound_used_keys")

    # Deduplicate reason labels while preserving order.
    reasons = list(dict.fromkeys(reasons))
    return bool(reasons), reasons, used_norm, claims_out


async def _repair_answer_contract(
    *,
    query: str,
    context: str,
    known_keys: Set[str],
    prior_answer: str,
    prior_reasons: List[str],
    model_key: Optional[str],
    user_id: Optional[int],
    fallback_candidates: Optional[List[str]],
) -> Dict[str, Any]:
    """One structural repair call: same evidence only, temperature=0, no re-retrieve."""
    allowed = json.dumps(sorted(known_keys), ensure_ascii=False)
    user_prompt = f"""参考内容（只能使用下列 reference_key；禁止重新检索或编造）:
{context}

允许的 reference_key 列表: {allowed}

用户问题: {query}

上一轮回答结构不合法（reasons={json.dumps(prior_reasons, ensure_ascii=False)}）。
请在不改变证据池的前提下，重新输出合法 JSON：answer + used_reference_keys + claims。
硬性要求：
- used_reference_keys 只能从允许列表选择，且必须是回答真正用到的证据
- 每条 claim.text 必须绑定至少一条 claim.reference_keys，且为 used_reference_keys 子集
- 每个 used key 必须被至少一条 claim 绑定
- answer 正文不要出现 key/ID
- 若证据确实无关才可拒答；相关则禁止空 claims / 空 used_reference_keys
"""
    llm = await generate_chat_completion(
        model_key=model_key or DEFAULT_MODEL_KEY,
        system=ANSWER_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
        mode="retrieval",
        max_tokens=4000,
        temperature=0.0,
        user_id=user_id,
        fallback_candidates=fallback_candidates,
    )
    structured = _parse_structured_answer(llm.content or "", known_context_keys=known_keys)
    structured["_contract_repair_attempted"] = True
    structured["_prior_answer_len"] = len(prior_answer or "")
    return structured


async def _generate_answer(
    query: str,
    context_references: List[Dict[str, Any]],
    conversation_history: Optional[List[Dict]] = None,
    model_key: Optional[str] = None,
    user_id: Optional[int] = None,
    fallback_candidates: Optional[List[str]] = None,
    *,
    system_policy: Optional[str] = None,
    answer_style: str = "default",
) -> Tuple[Dict[str, Any], Optional[Dict[str, str]]]:
    """Second LLM call: structured answer + used_reference_keys + claims."""
    if not context_references:
        return (
            {
                "answer": ABSTENTION_ANSWER,
                "used_reference_keys": [],
                "claims": [],
            },
            None,
        )

    annotated = annotate_references_with_keys(context_references)
    known_keys = {str(r["reference_key"]) for r in annotated}
    context, evidence_ledger = evidence_blocks_for_prompt(annotated, include_key=True)
    messages: List[Dict] = []
    if conversation_history:
        for msg in conversation_history[:-1]:
            role = msg["role"] if msg["role"] in ("user", "assistant") else "user"
            messages.append({"role": role, "content": msg["content"]})

    user_prompt = f"""参考内容（只能使用下列 reference_key；claims 必须原子化且绑定最小充分引用集）:
{context}

允许的 reference_key 列表: {json.dumps(sorted(known_keys), ensure_ascii=False)}

用户问题: {query}

请输出 JSON：answer + used_reference_keys + claims。
answer 正文不要出现 key/ID；used_reference_keys 必须是允许列表子集且为真正用到的证据。
不要时间→因果推断，不要局部记录概括成长期结论。
请逐条阅读全部参考后再决定是否拒答；任一参考相关则禁止拒答，短记录也算有效证据。"""
    messages.append({"role": "user", "content": user_prompt})

    try:
        llm = await generate_chat_completion(
            model_key=model_key or DEFAULT_MODEL_KEY,
            system=_compose_system_prompt(
                _answer_system_for_style(answer_style),
                system_policy=system_policy,
            ),
            messages=messages,
            mode="retrieval",
            max_tokens=4000,
            temperature=ANSWER_GENERATION_TEMPERATURE,
            user_id=user_id,
            fallback_candidates=fallback_candidates,
        )
        raw = llm.content or ""
        structured = _parse_structured_answer(raw, known_context_keys=known_keys)
        answer = structured.get("answer") or "AI 未能生成有效回答，请尝试换个问题。"
        try:
            from app.services.answer_style import apply_answer_style_guard

            answer, style_debug = apply_answer_style_guard(answer)
            if style_debug.get("changed"):
                logger.debug("answer style guard applied: %s", style_debug)
        except Exception as style_exc:
            logger.debug("answer style guard skipped: %s", style_exc)
        structured["answer"] = answer
        # Private ledger for eval alignment (hashes only are safe to persist publicly).
        structured["_evidence_sha_ledger"] = evidence_ledger

        # Factual answer without any valid used evidence → contract repair path (not silent abstention).
        used = list(structured.get("used_reference_keys") or [])
        looks_abstention = bool(re_search_abstention(answer))
        if answer.strip() and (not looks_abstention) and (not used):
            structured["_unsourced_factual_blocked"] = True

        return structured, _model_fallback_from_result(llm)
    except LLMGatewayError:
        raise
    except Exception as exc:
        logger.error(
            "第二次 LLM 调用失败: code=%s error=%s",
            "LLM_PROVIDER_FAILED",
            type(exc).__name__,
        )
        raise LLMGatewayError("AI 服务暂时不可用", code="LLM_PROVIDER_FAILED") from exc


async def generate_summary(
    query: str,
    references: List[Dict],
    conversation_history: Optional[List[Dict]] = None,
    model_key: Optional[str] = None,
    user_id: Optional[int] = None,
    fallback_candidates: Optional[List[str]] = None,
) -> str:
    """Legacy string summary wrapper."""
    result = await generate_structured_summary(
        query,
        references,
        conversation_history,
        model_key=model_key,
        user_id=user_id,
        fallback_candidates=fallback_candidates,
    )
    return result.get("answer", "AI 未能生成回答，请尝试换个问题。")


async def generate_structured_summary(
    query: str,
    references: List[Dict],
    conversation_history: Optional[List[Dict]] = None,
    model_key: Optional[str] = None,
    user_id: Optional[int] = None,
    fallback_candidates: Optional[List[str]] = None,
    *,
    context_strategy: str = DEFAULT_CONTEXT_STRATEGY,
    system_policy: Optional[str] = None,
    answer_style: str = "default",
) -> Dict:
    """
    Three-layer generation:
      retrieved_candidates → context_references → used_references (valid_references)

    Default strategy: deterministic source-aware context + structured answer.
    hard_judge is comparison-only and must not be the sole production path.
    """
    resolved_model_key = model_key or DEFAULT_MODEL_KEY
    strategy = (context_strategy or DEFAULT_CONTEXT_STRATEGY).strip() or DEFAULT_CONTEXT_STRATEGY

    retrieved = annotate_references_with_keys(references)
    pool_meta = {
        "relevance_temperature": RELEVANCE_JUDGE_TEMPERATURE,
        "answer_temperature": ANSWER_GENERATION_TEMPERATURE,
        "context_strategy": strategy,
        "retrieved_candidates_count": len(retrieved),
        "candidate_reference_keys": [r.get("reference_key") for r in retrieved],
        "candidates_private": [
            {
                "reference_key": r.get("reference_key"),
                "rank": idx + 1,
                "retrieval_method": r.get("retrieval_method") or r.get("pool_bucket"),
                "source_type": r.get("source_type"),
                "evidence_origin_key": r.get("evidence_origin_key"),
            }
            for idx, r in enumerate(retrieved)
        ],
    }

    if not retrieved:
        return {
            "answer": ABSTENTION_ANSWER,
            "valid_references": [],
            "invalid_references": [],
            "context_references": [],
            "used_reference_keys": [],
            "claims": [],
            "model_fallback": None,
            **pool_meta,
        }

    judge_result: Dict[str, Any] = {}
    judge_fallback = None
    invalid_refs: List[Dict[str, Any]] = []
    context_refs: List[Dict[str, Any]] = []
    context_meta: Dict[str, Any] = {}

    if strategy == CONTEXT_STRATEGY_HARD_JUDGE:
        logger.info("hard_judge: scoring %s retrieved candidates", len(retrieved))
        judge_result, judge_fallback = await _judge_relevance(
            query,
            retrieved,
            conversation_history,
            model_key=resolved_model_key,
            user_id=user_id,
            fallback_candidates=fallback_candidates,
            system_policy=system_policy,
        )
        judged_refs, invalid_refs = select_refs_by_judge_result(retrieved, judge_result)
        # Same total budget as deterministic / soft_judge paths.
        if judged_refs:
            context_refs, context_meta = build_source_aware_context(judged_refs)
            context_meta["strategy"] = strategy
            context_meta["hard_judge_pre_budget_count"] = len(judged_refs)
        else:
            context_refs, context_meta = build_source_aware_context(retrieved)
            context_meta["hard_judge_empty_fallback"] = "deterministic"
    elif strategy == CONTEXT_STRATEGY_SOFT_JUDGE:
        context_refs, context_meta = build_source_aware_context(retrieved)
        try:
            judge_result, judge_fallback = await _judge_relevance(
                query,
                context_refs,
                conversation_history,
                model_key=resolved_model_key,
                user_id=user_id,
                fallback_candidates=fallback_candidates,
                system_policy=system_policy,
            )
            scores = judge_result.get("_relevance_scores") or {}
            # Soft signal: reorder only; never drop to empty.
            context_refs = sorted(
                context_refs,
                key=lambda r: int(scores.get(str(r.get("reference_key")), 0)),
                reverse=True,
            )
            context_meta["soft_judge_applied"] = True
        except LLMGatewayError:
            # Soft judge failure must not block answering.
            context_meta["soft_judge_applied"] = False
            judge_fallback = None
    else:
        context_refs, context_meta = build_source_aware_context(retrieved)

    pool_meta["context_builder"] = context_meta
    pool_meta["context_reference_keys"] = [r.get("reference_key") for r in context_refs]

    if not context_refs:
        return {
            "answer": ABSTENTION_ANSWER,
            "valid_references": [],
            "invalid_references": invalid_refs,
            "context_references": [],
            "used_reference_keys": [],
            "claims": [],
            "model_fallback": judge_fallback,
            "judge_relevance_scores": judge_result.get("_relevance_scores") or {},
            **pool_meta,
        }

    logger.info(
        "structured answer over %s context refs (retrieved=%s strategy=%s)",
        len(context_refs),
        len(retrieved),
        strategy,
    )
    structured, answer_fallback = await _generate_answer(
        query,
        context_refs,
        conversation_history,
        model_key=resolved_model_key,
        user_id=user_id,
        fallback_candidates=fallback_candidates,
        system_policy=system_policy,
        answer_style=answer_style,
    )

    used_keys = list(structured.get("used_reference_keys") or [])
    by_key = {str(r.get("reference_key")): r for r in annotate_references_with_keys(context_refs)}
    known_context_keys = set(by_key.keys())
    answer_text = str(structured.get("answer") or "")
    claims = list(structured.get("claims") or [])
    evidence_sha_ledger = list(structured.get("_evidence_sha_ledger") or [])
    # Never auto-fabricate claims/citations from candidates.
    claim_contract_invalid, contract_reasons, used_keys, claims = _evaluate_claim_contract(
        answer_text=answer_text,
        used_keys=used_keys,
        claims=claims,
        known_keys=known_context_keys,
    )
    if structured.get("_unsourced_factual_blocked") and not re_search_abstention(answer_text):
        claim_contract_invalid = True
        if "used_reference_keys_empty" not in contract_reasons:
            contract_reasons.append("used_reference_keys_empty")

    if claim_contract_invalid and not re_search_abstention(answer_text):
        context_for_repair, _ = evidence_blocks_for_prompt(
            annotate_references_with_keys(context_refs),
            include_key=True,
        )
        try:
            repaired = await _repair_answer_contract(
                query=query,
                context=context_for_repair,
                known_keys=known_context_keys,
                prior_answer=answer_text,
                prior_reasons=contract_reasons,
                model_key=resolved_model_key,
                user_id=user_id,
                fallback_candidates=fallback_candidates,
            )
            answer_text = str(repaired.get("answer") or "")
            used_keys = list(repaired.get("used_reference_keys") or [])
            claims = list(repaired.get("claims") or [])
            claim_contract_invalid, contract_reasons, used_keys, claims = _evaluate_claim_contract(
                answer_text=answer_text,
                used_keys=used_keys,
                claims=claims,
                known_keys=known_context_keys,
            )
            structured["_contract_repair_attempted"] = True
        except LLMGatewayError:
            raise
        except Exception as repair_exc:
            logger.warning(
                "contract repair failed: error=%s",
                type(repair_exc).__name__,
            )
            raise LLMGatewayError(
                "回答结构校验失败",
                code="GENERATION_CONTRACT_FAILED",
            ) from repair_exc

        if claim_contract_invalid and not re_search_abstention(answer_text):
            # Must not return 200 with empty citations or fake abstention.
            raise LLMGatewayError(
                "回答结构校验失败",
                code="GENERATION_CONTRACT_FAILED",
            )

    if claim_contract_invalid:
        structured["_claim_contract_invalid"] = True
        structured["_claim_contract_reasons"] = contract_reasons

    used_refs_full = [by_key[k] for k in used_keys if k in by_key]
    used_refs_full, mirror_dropped = dedupe_by_evidence_origin(used_refs_full)
    for drop in mirror_dropped:
        invalid_refs.append(
            {
                "entry_id": drop.get("entry_id"),
                "reference_key": drop.get("reference_key"),
                "source_type": drop.get("source_type"),
                "evidence_origin_key": drop.get("evidence_origin_key"),
                "reason": "duplicate_evidence_mirror",
            }
        )
    # Unused context is NOT shown as citation cards.
    shown_keys = {str(r.get("reference_key")) for r in used_refs_full}
    for key in by_key:
        if key in shown_keys:
            continue
        ref = by_key[key]
        invalid_refs.append(
            {
                "entry_id": ref.get("entry_id"),
                "reference_key": key,
                "source_type": ref.get("source_type"),
                "reason": "context_not_used_in_answer",
            }
        )

    valid_refs = [format_valid_reference(ref) for ref in used_refs_full]
    return {
        "answer": answer_text or ABSTENTION_ANSWER,
        "valid_references": valid_refs,
        "invalid_references": invalid_refs,
        "context_references": [format_valid_reference(r) for r in context_refs],
        "used_reference_keys": [r.get("reference_key") for r in valid_refs],
        "claims": claims,
        "model_fallback": answer_fallback or judge_fallback,
        "judge_relevance_scores": judge_result.get("_relevance_scores") or {},
        "unsourced_factual_blocked": bool(structured.get("_unsourced_factual_blocked")),
        "claim_contract_invalid": bool(structured.get("_claim_contract_invalid")),
        "claim_contract_reasons": list(structured.get("_claim_contract_reasons") or []),
        # Public-safe hashes proving answer saw these evidence bodies (no body text).
        "evidence_sha_ledger": [
            {
                "reference_key": row.get("reference_key"),
                "evidence_sha256": row.get("evidence_sha256"),
                "evidence_char_limit": row.get("evidence_char_limit"),
            }
            for row in evidence_sha_ledger
            if isinstance(row, dict)
        ],
        **pool_meta,
    }
