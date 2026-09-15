"""
Generation quality metrics (Phase E.1).

Rules:
- Prefer explicit reference_key; else rank_fusion.result_key.
- Never rewrite an explicit short key (e.g. attachment_chunk:1) into
  attachment_chunk:chunk:1 when reporting; aliases are only for matching.
- Empty claims => faithfulness null/unsupported (not 1.0).
- Provider infra errors excluded from quality means.
"""
from __future__ import annotations

import re
from statistics import mean
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from app.services.answer_style import (
    detect_template_phrases,
    has_internal_leak,
    has_template_voice,
)
from app.services.reference_identity import (
    canonical_reference_key,
    key_match_aliases,
    keys_intersect,
)

# Strong refusals: clear "no relevant content" templates.
STRONG_ABSTENTION_RE = re.compile(
    r"(在您的记录中没有找到|没有找到与这个问题相关|没有与这个问题相关的内容|"
    r"未找到与.*相关|没有找到相关内容)"
)
# Weak hedges may appear inside otherwise factual answers; only count as
# abstention when the whole answer is a short hedge-only refusal.
WEAK_ABSTENTION_RE = re.compile(
    r"(没有找到|未找到|资料不足|证据不足|无法确认|缺少.*信息|暂时不能判断)"
)
NO_ANSWER_RE = STRONG_ABSTENTION_RE  # backward-compatible alias for imports

INFRA_ERROR_CODES = frozenset(
    {
        "E_PROVIDER_503",
        "E_PROVIDER_429",
        "E_LLM_NOT_CONFIGURED",
        "E_RETRIEVAL_INFRA",
        "E_JUDGE_INFRA",
        "E_GENERATION_INFRA",
        "LLM_RATE_LIMITED",
        "LLM_PROVIDER_UNAVAILABLE",
        "LLM_NOT_CONFIGURED",
    }
)

ERROR_LAYER_KEYS = (
    "retrieval_error",
    "judge_error",
    "generation_error",
    "parse_error",
    "infra_error",
)


def extract_reference_key(ref: Dict[str, Any]) -> Optional[str]:
    """
    Prefer explicit reference_key when present (may be legacy short form for
    historical artifacts). New production output uses canonical keys.
    """
    if not isinstance(ref, dict):
        return None
    explicit = ref.get("reference_key")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    try:
        return canonical_reference_key(ref)
    except Exception:
        return None


def expected_relevant_keys(case: Dict[str, Any]) -> Set[str]:
    keys: Set[str] = set()
    for key in case.get("expected_reference_keys") or []:
        if str(key).strip():
            keys.add(str(key).strip())
    for qrel in case.get("qrels") or []:
        if not isinstance(qrel, dict):
            continue
        try:
            rel = int(qrel.get("relevance", 0))
        except (TypeError, ValueError):
            rel = 0
        if rel <= 0:
            continue
        rk = str(qrel.get("reference_key") or "").strip()
        if rk:
            keys.add(rk)
    return keys


def is_abstention(answer: str) -> bool:
    """
    True only for genuine refusals.

    Factual answers that hedge with phrases like「无法确认」must NOT be
    treated as false abstention when they still provide grounded content.
    """
    text = (answer or "").strip()
    if not text:
        return True
    if STRONG_ABSTENTION_RE.search(text):
        return True
    # Short hedge-only replies (no substantive grounded answer).
    if WEAK_ABSTENTION_RE.search(text) and len(text) <= 40:
        return True
    return False


def _classify_infra(case: Dict[str, Any]) -> Optional[str]:
    verifier_infra = case.get("verifier_infra_error") or (
        (case.get("claim_verifier") or {}).get("infra_error")
        if isinstance(case.get("claim_verifier"), dict)
        else None
    )
    if verifier_infra:
        return str(verifier_infra)
    for key in ERROR_LAYER_KEYS:
        val = case.get(key)
        if not val:
            continue
        text = str(val)
        upper = text.upper()
        if "503" in text or "429" in text:
            return text
        for code in INFRA_ERROR_CODES:
            if code in upper or code in text:
                return text
        # Any layered provider/infra tag counts
        if key == "infra_error":
            return text
        if any(tok in upper for tok in ("RATE", "UNAVAILABLE", "TIMEOUT", "GATEWAY", "VERIFIER")):
            return text
    err = case.get("error_code") or case.get("provider_error")
    if err:
        text = str(err)
        upper = text.upper()
        if "503" in text or "429" in text or any(c in upper for c in INFRA_ERROR_CODES):
            return text
    return None


def _evaluate_claims(
    claims: Sequence[Dict[str, Any]],
    returned_keys: Set[str],
    corpus: str,
) -> Dict[str, Any]:
    claim_results: List[Dict[str, Any]] = []
    must_total = 0
    supported = 0
    unsupported = 0

    for claim in claims:
        if not isinstance(claim, dict):
            continue
        support_terms = [
            str(t) for t in (claim.get("support_terms") or []) if str(t).strip()
        ]
        required = {
            str(k)
            for k in (claim.get("required_reference_keys") or [])
            if str(k).strip()
        }
        must = bool(claim.get("must_be_supported", True))
        missing_terms = [t for t in support_terms if t.lower() not in corpus]
        ref_ok = True
        if required:
            ref_ok = bool(keys_intersect(required, returned_keys))
        ok = (not missing_terms) and ref_ok
        reasons: List[str] = []
        if missing_terms:
            reasons.append("missing_support_terms")
        if not ref_ok:
            reasons.append("required_reference_key_missing")
        claim_results.append(
            {
                "must_be_supported": must,
                "supported": ok,
                "reasons": reasons,
                # Privacy: omit claim text from default public view; caller may keep.
                "has_text": bool(str(claim.get("text") or "").strip()),
            }
        )
        if not must:
            continue
        must_total += 1
        if ok:
            supported += 1
        else:
            unsupported += 1

    if must_total == 0:
        # Empty / no must-support claims => unsupported faithfulness (not 1.0)
        return {
            "claim_count": len(claim_results),
            "must_be_supported_claim_count": 0,
            "supported_claim_count": 0,
            "unsupported_claim_count": 0,
            "claim_coverage": None,
            "faithfulness_score": None,
            "faithfulness_status": "unsupported",
            "claim_results": claim_results,
        }

    return {
        "claim_count": len(claim_results),
        "must_be_supported_claim_count": must_total,
        "supported_claim_count": supported,
        "unsupported_claim_count": unsupported,
        "claim_coverage": supported / must_total,
        "faithfulness_score": supported / must_total,
        "faithfulness_status": "scored",
        "claim_results": claim_results,
    }


def _reference_corpus(references: Sequence[Dict[str, Any]]) -> str:
    parts: List[str] = []
    for ref in references:
        if not isinstance(ref, dict):
            continue
        for key in ("title", "snippet", "content_text", "text", "summary"):
            val = ref.get(key)
            if isinstance(val, str) and val.strip():
                parts.append(val.strip())
        meta = ref.get("metadata")
        if isinstance(meta, dict):
            for key in ("caption", "ocr_text"):
                val = meta.get(key)
                if isinstance(val, str) and val.strip():
                    parts.append(val.strip())
    return " ".join(parts).lower()


def _keys_from_refs(refs: Any) -> Set[str]:
    out: Set[str] = set()
    if not isinstance(refs, list):
        return out
    for ref in refs:
        if isinstance(ref, dict):
            key = extract_reference_key(ref)
            if key:
                out.add(key)
        elif isinstance(ref, str) and ref.strip():
            out.add(ref.strip())
    return out


def _layered_qrel_metrics(
    *,
    expected_keys: Set[str],
    retrieved_keys: Set[str],
    context_keys: Set[str],
    used_keys: Set[str],
    expected_answerable: bool,
    infra: Optional[str],
) -> Dict[str, Optional[float]]:
    """Qrel-layer diagnostics only. final_citation_precision is claim-based elsewhere."""
    if infra:
        return {
            "retrieval_qrel_recall": None,
            "context_qrel_precision": None,
            "context_qrel_recall": None,
            "qrel_reference_recall": None,
        }
    if not expected_answerable:
        return {
            "retrieval_qrel_recall": None,
            "context_qrel_precision": 1.0 if not used_keys else 0.0,
            "context_qrel_recall": None,
            "qrel_reference_recall": None,
        }
    if not expected_keys:
        return {
            "retrieval_qrel_recall": None,
            "context_qrel_precision": 1.0 if context_keys else 0.0,
            "context_qrel_recall": None,
            "qrel_reference_recall": None,
        }
    ret_hit = keys_intersect(retrieved_keys, expected_keys)
    ctx_hit = keys_intersect(context_keys, expected_keys)
    used_hit = keys_intersect(used_keys, expected_keys)
    return {
        "retrieval_qrel_recall": len(ret_hit) / len(expected_keys),
        "context_qrel_precision": (
            len(keys_intersect(context_keys, expected_keys)) / max(len(context_keys), 1)
            if context_keys
            else 0.0
        ),
        "context_qrel_recall": len(ctx_hit) / len(expected_keys),
        # Diagnostic: old "every qrel must be shown" recall — not a hard gate.
        "qrel_reference_recall": len(used_hit) / len(expected_keys),
    }


def _citation_linkage_precision(
    used_keys: Set[str],
    claims: Sequence[Dict[str, Any]],
    *,
    expected_answerable: bool,
    abstained: bool,
    infra: Optional[str],
) -> Optional[float]:
    """Structural: used citation bound by ≥1 claim. NOT a close gate."""
    if infra:
        return None
    if not expected_answerable:
        return 1.0 if not used_keys else 0.0
    if abstained:
        return 1.0 if not used_keys else 0.0
    if not used_keys:
        return 0.0
    claim_keys: Set[str] = set()
    for claim in claims or []:
        if not isinstance(claim, dict):
            continue
        for key in claim.get("reference_keys") or []:
            if str(key).strip():
                claim_keys.add(str(key).strip())
    if not claim_keys:
        return 0.0
    justified = keys_intersect(used_keys, claim_keys)
    return len(justified) / max(len(used_keys), 1)


def _claim_linkage_coverage(
    claims: Sequence[Dict[str, Any]],
    used_keys: Set[str],
) -> Tuple[Optional[float], int, int]:
    """Structural: factual claim binds ≥1 used key. NOT a close gate."""
    total = 0
    supported = 0
    for claim in claims or []:
        if not isinstance(claim, dict):
            continue
        text = str(claim.get("text") or "").strip()
        if not text:
            continue
        total += 1
        claim_keys = {
            str(k).strip()
            for k in (claim.get("reference_keys") or [])
            if str(k).strip()
        }
        if claim_keys and keys_intersect(claim_keys, used_keys):
            supported += 1
    if total == 0:
        if used_keys:
            return 0.0, 0, 0
        return None, 0, 0
    return supported / total, supported, total


def _semantic_final_citation_precision(
    used_keys: Set[str],
    supporting_keys: Set[str],
    *,
    expected_answerable: bool,
    abstained: bool,
    infra: Optional[str],
    verifier_infra: Optional[str],
) -> Optional[float]:
    """Citations whose evidence body supports ≥1 claim / final citation count."""
    if infra or verifier_infra:
        return None
    if not expected_answerable:
        return 1.0 if not used_keys else 0.0
    if abstained:
        return 1.0 if not used_keys else 0.0
    if not used_keys:
        return 0.0
    hit = keys_intersect(used_keys, supporting_keys)
    return len(hit) / max(len(used_keys), 1)


def _semantic_claim_support_coverage(
    factual_claim_count: int,
    supported_claim_count: int,
    *,
    expected_answerable: bool,
    abstained: bool,
    infra: Optional[str],
    verifier_infra: Optional[str],
    used_keys: Set[str],
) -> Optional[float]:
    """Claims with ≥1 citation judged supported / all factual claims."""
    if infra or verifier_infra:
        return None
    if not expected_answerable:
        return 1.0 if not used_keys else 0.0
    if abstained:
        return 1.0 if not used_keys else 0.0
    if factual_claim_count <= 0:
        return 0.0 if used_keys else None
    return supported_claim_count / factual_claim_count


def score_generation_case(case: Dict[str, Any]) -> Dict[str, Any]:
    answer = str(case.get("answer") or "")
    references = case.get("references") or case.get("valid_references") or []
    if not isinstance(references, list):
        references = []
    expected_answerable = bool(case.get("expected_answerable", True))
    expected_keys = expected_relevant_keys(case)
    returned_keys_list: List[str] = []
    for ref in references:
        key = extract_reference_key(ref) if isinstance(ref, dict) else None
        if key:
            returned_keys_list.append(key)
    returned_keys = set(returned_keys_list)
    # Prefer explicit used keys when present (architecture-corrected path).
    used_explicit = case.get("used_reference_keys")
    if isinstance(used_explicit, list) and used_explicit:
        used_keys = {str(k).strip() for k in used_explicit if str(k).strip()}
    else:
        used_keys = set(returned_keys)
    retrieved_keys = _keys_from_refs(case.get("retrieved_candidates") or case.get("candidates_private"))
    if not retrieved_keys:
        retrieved_keys = _keys_from_refs(
            [{"reference_key": k} for k in (case.get("candidate_reference_keys") or [])]
        )
    context_keys = _keys_from_refs(case.get("context_references"))
    if not context_keys:
        context_keys = _keys_from_refs(
            [{"reference_key": k} for k in (case.get("context_reference_keys") or [])]
        )
    if not context_keys:
        # Legacy: treat returned citations as both context and used.
        context_keys = set(returned_keys)

    infra = _classify_infra(case)
    ranking_eligible = infra is None

    abstained = is_abstention(answer)
    if infra:
        abstention_correct: Optional[float] = None
        abstention_status = "infra_excluded"
    elif expected_answerable:
        abstention_correct = 0.0 if abstained else 1.0
        abstention_status = "false_abstention" if abstained else "answered"
    else:
        abstention_correct = 1.0 if abstained else 0.0
        abstention_status = "correct_abstention" if abstained else "missed_abstention"

    matched_returned = keys_intersect(used_keys or returned_keys, expected_keys)
    matched_expected = keys_intersect(expected_keys, used_keys or returned_keys)

    # Legacy original-qrels citation P/R (kept for dual reporting; not deleted).
    if infra:
        citation_precision: Optional[float] = None
        citation_recall: Optional[float] = None
        citation_coverage: Optional[float] = None
    elif expected_answerable and expected_keys:
        cite_keys = used_keys or returned_keys
        citation_precision = (
            len(keys_intersect(cite_keys, expected_keys)) / max(len(cite_keys), 1)
            if cite_keys
            else 0.0
        )
        citation_recall = len(keys_intersect(expected_keys, cite_keys)) / len(expected_keys)
        citation_coverage = citation_recall
    elif expected_answerable and not expected_keys:
        citation_precision = 1.0 if (used_keys or returned_keys) else 0.0
        citation_recall = None
        citation_coverage = 1.0 if (used_keys or returned_keys) else 0.0
    else:
        citation_precision = 1.0 if not (used_keys or returned_keys) else 0.0
        citation_recall = None
        citation_coverage = 1.0 if not (used_keys or returned_keys) else 0.0

    cite_keys = used_keys or returned_keys
    layered = _layered_qrel_metrics(
        expected_keys=expected_keys,
        retrieved_keys=retrieved_keys,
        context_keys=context_keys,
        used_keys=cite_keys,
        expected_answerable=expected_answerable,
        infra=infra,
    )

    internal_leak = has_internal_leak(answer)
    template_voice = has_template_voice(answer)
    template_phrases = detect_template_phrases(answer)
    natural_language = (
        bool(answer.strip())
        and not internal_leak
        and not template_voice
        and "```" not in answer
    )
    answer_len = len(answer.strip())
    brevity_ok = 0 < answer_len <= 1200

    raw_claims = case.get("claims", None)
    if isinstance(raw_claims, list):
        claims = raw_claims
    else:
        claims = []
    claim_metrics = _evaluate_claims(claims, cite_keys, _reference_corpus(references))
    # Structural linkage (previous-round FCP/CSC; dual-reported, not close gates).
    citation_linkage_precision = _citation_linkage_precision(
        cite_keys,
        claims,
        expected_answerable=expected_answerable,
        abstained=abstained,
        infra=infra,
    )
    claim_linkage_coverage, claim_supported_n, claim_total_n = _claim_linkage_coverage(
        claims,
        cite_keys,
    )
    # Keep old field names as aliases for structural metrics (do not hide).
    final_citation_precision = citation_linkage_precision
    claim_support = claim_linkage_coverage

    verifier = case.get("claim_verifier") if isinstance(case.get("claim_verifier"), dict) else {}
    verifier_infra = case.get("verifier_infra_error") or verifier.get("infra_error")
    supporting_keys = {
        str(k).strip()
        for k in (verifier.get("semantically_supporting_keys") or [])
        if str(k).strip()
    }
    # Also accept per-citation flags if provided.
    for ref in references:
        if isinstance(ref, dict) and ref.get("semantically_supports_claim"):
            key = extract_reference_key(ref)
            if key:
                supporting_keys.add(key)
    factual_n = int(verifier.get("factual_claim_count") or claim_total_n or 0)
    supported_n = int(verifier.get("supported_claim_count") or 0)
    # If verifier ran with verdicts list, prefer that count.
    verdicts = verifier.get("claim_verdicts")
    if isinstance(verdicts, list) and verdicts:
        factual_n = len(verdicts)
        supported_n = sum(1 for v in verdicts if v.get("supported_by_any") is True)

    semantic_fcp = _semantic_final_citation_precision(
        cite_keys,
        supporting_keys,
        expected_answerable=expected_answerable,
        abstained=abstained,
        infra=infra,
        verifier_infra=str(verifier_infra) if verifier_infra else None,
    )
    semantic_csc = _semantic_claim_support_coverage(
        factual_n,
        supported_n,
        expected_answerable=expected_answerable,
        abstained=abstained,
        infra=infra,
        verifier_infra=str(verifier_infra) if verifier_infra else None,
        used_keys=cite_keys,
    )
    # Missing verifier on answered case → semantic metrics null (cannot pass).
    # Default False for unit/legacy offline fixtures; live semantic batch sets True.
    if (
        expected_answerable
        and (not abstained)
        and (not infra)
        and (not verifier)
        and bool(case.get("require_semantic_verifier"))
    ):
        semantic_fcp = None
        semantic_csc = None
    elif (
        expected_answerable
        and (not abstained)
        and (not infra)
        and (not verifier)
        and not bool(case.get("require_semantic_verifier"))
    ):
        # Offline structural-only scoring: leave semantic null without failing close
        # unless the caller explicitly requires the verifier.
        semantic_fcp = None
        semantic_csc = None
    # Manual / judge overrides for faithfulness when present
    manual = case.get("manual_scores") or {}
    judge = case.get("judge_scores") or {}
    faithfulness_score = claim_metrics["faithfulness_score"]
    faithfulness_status = claim_metrics["faithfulness_status"]
    if faithfulness_score is None:
        if isinstance(manual.get("faithfulness"), (int, float)):
            faithfulness_score = float(manual["faithfulness"]) / (
                5.0 if float(manual["faithfulness"]) > 1.0 else 1.0
            )
            faithfulness_score = max(0.0, min(1.0, faithfulness_score))
            faithfulness_status = "manual"
        elif isinstance(judge.get("faithfulness"), (int, float)):
            faithfulness_score = float(judge["faithfulness"])
            faithfulness_status = str(
                case.get("judge_provenance") or "llm_judge"
            )

    failures: List[str] = []
    if infra:
        text = str(infra).upper()
        if "429" in text or "503" in text or "PROVIDER" in text or "LLM_NOT_CONFIGURED" in text:
            failures.append("provider_infra_error")
        elif "RETRIEVAL" in text:
            failures.append("retrieval_infra_error")
        elif "RUNTIME" in text or "TYPEERROR" in text:
            failures.append("generation_runtime_error")
        else:
            failures.append("infra_error")
    else:
        if internal_leak:
            failures.append("internal_field_leak")
        if template_voice:
            failures.append("template_voice")
        if expected_answerable and abstained:
            failures.append("false_abstention")
        if (not expected_answerable) and (not abstained):
            failures.append("missed_abstention")
        if case.get("claim_contract_invalid"):
            failures.append("claim_contract_invalid")
        # Close-gate semantic metrics (null ⇒ fail only when verifier required).
        if expected_answerable and (not abstained) and bool(case.get("require_semantic_verifier")):
            if semantic_fcp is None:
                failures.append("semantic_final_citation_precision_missing")
            elif semantic_fcp < 1.0:
                failures.append("semantic_final_citation_precision_low")
            if semantic_csc is None:
                failures.append("semantic_claim_support_coverage_missing")
            elif semantic_csc < 1.0:
                failures.append("semantic_claim_support_coverage_low")
        # Structural linkage retained (not close gates).
        if (
            expected_answerable
            and (not abstained)
            and citation_linkage_precision is not None
            and citation_linkage_precision < 1.0
        ):
            failures.append("citation_linkage_precision_low")
        if (
            expected_answerable
            and (not abstained)
            and claim_linkage_coverage is not None
            and claim_linkage_coverage < 1.0
        ):
            failures.append("claim_linkage_coverage_low")
        # Legacy original-qrels flags retained for dual reporting (not deleted).
        if expected_answerable and citation_precision is not None and citation_precision < 1.0:
            failures.append("citation_precision_low")
        if expected_answerable and citation_recall is not None and citation_recall < 1.0:
            failures.append("citation_recall_low")
        if (
            claim_metrics["faithfulness_status"] == "scored"
            and claim_metrics["unsupported_claim_count"]
            > int(case.get("expected_unsupported_claims_max", 0))
        ):
            failures.append("unsupported_claims")

    return {
        "case_id": case.get("case_id"),
        "expected_answerable": expected_answerable,
        "ranking_eligible": ranking_eligible,
        "infra_error": infra,
        "reference_count": len(references),
        "reference_keys": returned_keys_list,
        "expected_reference_keys": sorted(expected_keys),
        "matched_reference_keys": sorted(matched_returned),
        # Legacy dual report (original-qrels style on final used citations).
        "citation_precision": citation_precision,
        "citation_recall": citation_recall,
        "citation_coverage": citation_coverage,
        # Architecture / qrel diagnostics.
        "retrieval_qrel_recall": layered["retrieval_qrel_recall"],
        "context_qrel_precision": layered["context_qrel_precision"],
        "context_qrel_recall": layered["context_qrel_recall"],
        "qrel_reference_recall": layered["qrel_reference_recall"],
        # Structural linkage (also aliased as previous-round FCP/CSC names).
        "citation_linkage_precision": citation_linkage_precision,
        "claim_linkage_coverage": claim_linkage_coverage,
        "final_citation_precision": final_citation_precision,
        "claim_support_coverage": claim_support,
        "claim_support_supported_count": claim_supported_n,
        "claim_support_total_count": claim_total_n,
        # Semantic close-gate metrics.
        "semantic_final_citation_precision": semantic_fcp,
        "semantic_claim_support_coverage": semantic_csc,
        "verifier_provenance": verifier.get("verifier_provenance"),
        "verifier_infra_error": verifier_infra,
        "abstained": abstained,
        "abstention_correct": abstention_correct,
        "abstention_status": abstention_status,
        "internal_field_leak": 1.0 if internal_leak else 0.0,
        "template_voice": 1.0 if template_voice else 0.0,
        "template_phrases": template_phrases,
        "natural_language": 1.0 if natural_language else 0.0,
        "brevity_ok": 1.0 if brevity_ok else 0.0,
        "answer_length": answer_len,
        "claim_count": claim_metrics["claim_count"],
        "must_be_supported_claim_count": claim_metrics["must_be_supported_claim_count"],
        "supported_claim_count": claim_metrics["supported_claim_count"],
        "unsupported_claim_count": claim_metrics["unsupported_claim_count"],
        "claim_coverage": claim_metrics["claim_coverage"],
        "faithfulness_score": faithfulness_score,
        "faithfulness_status": faithfulness_status,
        "judge_scores": judge or None,
        "judge_provenance": case.get("judge_provenance"),
        "manual_scores": manual or None,
        "failures": failures,
        "error_layers": {
            k: case.get(k) for k in ERROR_LAYER_KEYS if case.get(k)
        },
    }


def public_case_view(
    scored: Dict[str, Any],
    *,
    include_content: bool = False,
    raw_case: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Strip query/answer/body fields unless include_content."""
    out = dict(scored)
    # Never put raw content into public view by default
    for key in ("query", "answer", "snippet", "title", "filename", "claims", "claim_results"):
        out.pop(key, None)
    if include_content and raw_case is not None:
        out["query"] = raw_case.get("query")
        out["answer"] = raw_case.get("answer")
        # Still avoid dumping full reference bodies unless present on scored
    return out


def _safe_mean(values: List[float]) -> Optional[float]:
    if not values:
        return None
    return float(mean(values))


def aggregate_generation_report(
    rows: Sequence[Dict[str, Any]],
    *,
    include_content: bool = False,
    raw_by_id: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    quality_rows = [r for r in rows if r.get("ranking_eligible")]
    infra_rows = [r for r in rows if not r.get("ranking_eligible")]
    answerable = [r for r in quality_rows if r.get("expected_answerable")]
    no_answer = [r for r in quality_rows if not r.get("expected_answerable")]

    def col(subset: Sequence[Dict[str, Any]], key: str) -> List[float]:
        vals: List[float] = []
        for row in subset:
            v = row.get(key)
            if isinstance(v, (int, float)):
                vals.append(float(v))
        return vals

    fail_counts: Dict[str, int] = {}
    for row in rows:
        for f in row.get("failures") or []:
            fail_counts[str(f)] = fail_counts.get(str(f), 0) + 1

    cases_out = [
        public_case_view(
            r,
            include_content=include_content,
            raw_case=(raw_by_id or {}).get(str(r.get("case_id") or "")),
        )
        for r in rows
    ]

    return {
        "case_count": len(rows),
        "infra_error_case_count": len(infra_rows),
        "quality_case_count": len(quality_rows),
        "answerable_denominator": len(answerable),
        "no_answer_denominator": len(no_answer),
        "averages": {
            # Legacy original-qrels (dual report; must remain visible).
            "citation_precision_answerable": _safe_mean(col(answerable, "citation_precision")),
            "citation_recall_answerable": _safe_mean(col(answerable, "citation_recall")),
            "citation_coverage_answerable": _safe_mean(col(answerable, "citation_coverage")),
            # Structural linkage (previous-round FCP/CSC aliases; not close gates).
            "citation_linkage_precision_answerable": _safe_mean(
                col(answerable, "citation_linkage_precision")
            ),
            "claim_linkage_coverage_answerable": _safe_mean(
                col(answerable, "claim_linkage_coverage")
            ),
            "final_citation_precision_answerable": _safe_mean(
                col(answerable, "final_citation_precision")
            ),
            "claim_support_coverage_answerable": _safe_mean(
                col(answerable, "claim_support_coverage")
            ),
            # Semantic close-gate metrics.
            "semantic_final_citation_precision_answerable": _safe_mean(
                col(answerable, "semantic_final_citation_precision")
            ),
            "semantic_claim_support_coverage_answerable": _safe_mean(
                col(answerable, "semantic_claim_support_coverage")
            ),
            "retrieval_qrel_recall_answerable": _safe_mean(
                col(answerable, "retrieval_qrel_recall")
            ),
            "context_qrel_precision_answerable": _safe_mean(
                col(answerable, "context_qrel_precision")
            ),
            "context_qrel_recall_answerable": _safe_mean(
                col(answerable, "context_qrel_recall")
            ),
            "qrel_reference_recall_answerable": _safe_mean(
                col(answerable, "qrel_reference_recall")
            ),
            "abstention_correct_answerable": _safe_mean(col(answerable, "abstention_correct")),
            "abstention_correct_no_answer": _safe_mean(col(no_answer, "abstention_correct")),
            "false_abstention_rate_answerable": (
                sum(1 for r in answerable if r.get("abstained")) / len(answerable)
                if answerable
                else None
            ),
            "correct_abstention_rate_no_answer": (
                sum(1 for r in no_answer if r.get("abstained")) / len(no_answer)
                if no_answer
                else None
            ),
            "internal_field_leak_rate": _safe_mean(col(quality_rows, "internal_field_leak")),
            "template_voice_rate": _safe_mean(col(quality_rows, "template_voice")),
            "natural_language_rate": _safe_mean(col(quality_rows, "natural_language")),
            "brevity_ok_rate": _safe_mean(col(quality_rows, "brevity_ok")),
            "faithfulness_score": _safe_mean(
                [
                    float(r["faithfulness_score"])
                    for r in quality_rows
                    if isinstance(r.get("faithfulness_score"), (int, float))
                ]
            ),
            "faithfulness_scored_count": sum(
                1
                for r in quality_rows
                if isinstance(r.get("faithfulness_score"), (int, float))
            ),
            "faithfulness_unsupported_count": sum(
                1
                for r in quality_rows
                if r.get("faithfulness_status") == "unsupported"
            ),
        },
        "denominators": {
            "all_cases": len(rows),
            "quality_cases": len(quality_rows),
            "infra_excluded": len(infra_rows),
            "answerable": len(answerable),
            "no_answer": len(no_answer),
        },
        "failure_type_counts": fail_counts,
        "quality_claim_allowed": False,
        "note": (
            "Infra errors excluded from quality means. "
            "citation_linkage_* / final_citation_precision are structural only; "
            "semantic_* require claim-evidence verifier; "
            "citation_precision/recall remain original-qrels dual report. "
            "Faithfulness null when claims/manual/judge absent. "
            "Default cases omit query/answer content."
        ),
        "cases": cases_out,
    }
