"""
Eval-only claim ↔ evidence semantic verifier v2.

Pair-level + set-level judgments. Does not run on production answer path.
Does not read qrels. temperature=0.
Failures → infra (semantic metrics must stay null).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Sequence

from app.services.evidence_text import (
    EVIDENCE_CHAR_LIMIT,
    annotate_evidence_serialization,
    serialize_evidence_body,
)
from app.services.llm_gateway import (
    DEFAULT_MODEL_KEY,
    LLMGatewayError,
    generate_chat_completion,
)

logger = logging.getLogger(__name__)

VERIFIER_TEMPERATURE = 0.0
VERIFIER_PROVENANCE = "deepseek_claim_evidence_verifier_v2"
VERIFIER_PROVENANCE_BLIND = "deepseek_claim_evidence_verifier_v2_blind"

PAIR_LABELS = frozenset({"supports", "contributes", "irrelevant", "contradicted"})
SET_LABELS = frozenset({"supported", "insufficient", "contradicted"})

PAIR_SYSTEM = """你是严格的 claim×单条证据 判定器。只根据该条证据正文判断。
禁止把 claim 自带的 reference_key / 模型自我声明当作证据。
只输出 JSON：
{"pair_verdict":"supports|contributes|irrelevant|contradicted","support_confidence":0.0,"support_span_found":true}
- supports：该条证据单独即可支持 claim 的核心事实
- contributes：对多证据 claim 有实际贡献，但单独不完整
- irrelevant：无关或不能贡献
- contradicted：与 claim 冲突"""

PAIR_SYSTEM_BLIND = """You are a strict claim-evidence pair judge. Use ONLY the evidence body.
Do not treat reference keys as evidence. Output JSON only:
{"pair_verdict":"supports|contributes|irrelevant|contradicted","support_confidence":0.0,"support_span_found":true}"""

SET_SYSTEM = """你是严格的 claim×证据集合 判定器。综合全部给定证据判定 claim。
禁止把 reference_key / 模型自我声明当作证据。
只输出 JSON：
{"set_verdict":"supported|insufficient|contradicted","support_confidence":0.0}
- supported：证据集合共同充分支持 claim
- insufficient：不足或无关
- contradicted：存在冲突且不能成立"""


def _strip_json(raw: str) -> str:
    text = (raw or "").strip()
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
    return text


def _parse_pair(raw: str) -> Dict[str, Any]:
    data = json.loads(_strip_json(raw))
    if not isinstance(data, dict):
        raise ValueError("pair_not_object")
    verdict = str(data.get("pair_verdict") or data.get("verdict") or "").strip().lower()
    if verdict not in PAIR_LABELS:
        raise ValueError(f"bad_pair_verdict:{verdict}")
    try:
        conf = max(0.0, min(1.0, float(data.get("support_confidence"))))
    except (TypeError, ValueError) as exc:
        raise ValueError("bad_confidence") from exc
    return {
        "pair_verdict": verdict,
        "support_confidence": conf,
        "support_span_found": bool(data.get("support_span_found")),
    }


def _parse_set(raw: str) -> Dict[str, Any]:
    data = json.loads(_strip_json(raw))
    if not isinstance(data, dict):
        raise ValueError("set_not_object")
    verdict = str(data.get("set_verdict") or data.get("verdict") or "").strip().lower()
    if verdict not in SET_LABELS:
        raise ValueError(f"bad_set_verdict:{verdict}")
    try:
        conf = max(0.0, min(1.0, float(data.get("support_confidence"))))
    except (TypeError, ValueError) as exc:
        raise ValueError("bad_confidence") from exc
    return {"set_verdict": verdict, "support_confidence": conf}


async def _llm_json(
    *,
    system: str,
    user_prompt: str,
    model_key: Optional[str],
    user_id: Optional[int],
) -> str:
    llm = await generate_chat_completion(
        model_key=model_key or DEFAULT_MODEL_KEY,
        system=system,
        messages=[{"role": "user", "content": user_prompt}],
        mode="retrieval",
        max_tokens=400,
        temperature=VERIFIER_TEMPERATURE,
        user_id=user_id,
    )
    return llm.content or ""


async def verify_claim_citation_pair(
    claim_text: str,
    citation: Dict[str, Any],
    *,
    model_key: Optional[str] = None,
    user_id: Optional[int] = None,
    blind: bool = False,
) -> Dict[str, Any]:
    claim_text = str(claim_text or "").strip()
    if not claim_text or not isinstance(citation, dict):
        return {
            "pair_verdict": "irrelevant",
            "support_confidence": 0.0,
            "support_span_found": False,
            "infra_error": None,
            "evidence_sha256": None,
        }
    ann = annotate_evidence_serialization([citation])[0]
    body = ann["evidence_body"]
    sha = ann["evidence_sha256"]
    key = str(citation.get("reference_key") or "")
    # Blind prompt omits key labels to reduce key-bias.
    if blind:
        user_prompt = (
            f"Claim:\n{claim_text}\n\nEvidence body:\n{body}\n\n"
            "Judge pair_verdict only from the evidence body."
        )
        system = PAIR_SYSTEM_BLIND
    else:
        user_prompt = (
            f"Claim:\n{claim_text}\n\n"
            f"证据正文（唯一依据；勿把 key 当证据）:\n{body}\n\n"
            f"(opaque_id={key or 'none'})\n"
            "请给出 pair_verdict。"
        )
        system = PAIR_SYSTEM
    try:
        raw = await _llm_json(
            system=system, user_prompt=user_prompt, model_key=model_key, user_id=user_id
        )
        parsed = _parse_pair(raw)
        return {
            **parsed,
            "infra_error": None,
            "evidence_sha256": sha,
            "reference_key": key,
        }
    except LLMGatewayError as exc:
        code = getattr(exc, "code", None) or "E_VERIFIER_PROVIDER"
        return {
            "pair_verdict": None,
            "support_confidence": None,
            "support_span_found": None,
            "infra_error": f"E_VERIFIER_INFRA:{code}",
            "evidence_sha256": sha,
            "reference_key": key,
        }
    except Exception:
        return {
            "pair_verdict": None,
            "support_confidence": None,
            "support_span_found": None,
            "infra_error": "E_VERIFIER_PARSE",
            "evidence_sha256": sha,
            "reference_key": key,
        }


async def verify_claim_citation_set(
    claim_text: str,
    citations: Sequence[Dict[str, Any]],
    *,
    model_key: Optional[str] = None,
    user_id: Optional[int] = None,
    reverse_order: bool = False,
    blind: bool = False,
) -> Dict[str, Any]:
    claim_text = str(claim_text or "").strip()
    cites = [c for c in citations if isinstance(c, dict)]
    if reverse_order:
        cites = list(reversed(cites))
    if not claim_text or not cites:
        return {
            "set_verdict": "insufficient",
            "support_confidence": 0.0,
            "infra_error": None,
            "evidence_sha256_list": [],
        }
    ann = annotate_evidence_serialization(cites)
    blocks = []
    sha_list = []
    for idx, ref in enumerate(ann, start=1):
        sha_list.append(str(ref["evidence_sha256"]))
        if blind:
            blocks.append(f"[Evidence {idx}]\n{ref['evidence_body']}")
        else:
            blocks.append(f"[证据{idx}]\n{ref['evidence_body']}")
    user_prompt = (
        f"Claim:\n{claim_text}\n\n"
        f"证据集合:\n" + "\n\n".join(blocks) + "\n\n"
        "综合判定 set_verdict。不要复述隐私细节。"
    )
    try:
        raw = await _llm_json(
            system=SET_SYSTEM, user_prompt=user_prompt, model_key=model_key, user_id=user_id
        )
        parsed = _parse_set(raw)
        return {
            **parsed,
            "infra_error": None,
            "evidence_sha256_list": sha_list,
        }
    except LLMGatewayError as exc:
        code = getattr(exc, "code", None) or "E_VERIFIER_PROVIDER"
        return {
            "set_verdict": None,
            "support_confidence": None,
            "infra_error": f"E_VERIFIER_INFRA:{code}",
            "evidence_sha256_list": sha_list,
        }
    except Exception:
        return {
            "set_verdict": None,
            "support_confidence": None,
            "infra_error": "E_VERIFIER_PARSE",
            "evidence_sha256_list": sha_list,
        }


async def verify_claim_against_evidence(
    claim_text: str,
    citations: Sequence[Dict[str, Any]],
    *,
    model_key: Optional[str] = None,
    user_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Backward-compatible wrapper: runs v2 pair+set and exposes set verdict as verdict.
    Supporting keys come ONLY from pair supports|contributes — never from set alone.
    """
    cites = [c for c in citations if isinstance(c, dict)]
    if not str(claim_text or "").strip() or not cites:
        return {
            "verdict": "insufficient",
            "set_verdict": "insufficient",
            "support_confidence": 0.0,
            "support_span_found": False,
            "verifier_provenance": VERIFIER_PROVENANCE,
            "infra_error": None,
            "supported_by_any": False,
            "pair_results": [],
            "supporting_reference_keys": [],
        }

    pair_results: List[Dict[str, Any]] = []
    infra: Optional[str] = None
    supporting_keys: List[str] = []
    for cite in cites:
        pair = await verify_claim_citation_pair(
            claim_text, cite, model_key=model_key, user_id=user_id
        )
        if pair.get("infra_error") and not infra:
            infra = str(pair["infra_error"])
        key = str(cite.get("reference_key") or pair.get("reference_key") or "").strip()
        row = {
            "reference_key": key,
            "pair_verdict": pair.get("pair_verdict"),
            "support_confidence": pair.get("support_confidence"),
            "support_span_found": pair.get("support_span_found"),
            "evidence_sha256": pair.get("evidence_sha256"),
            "infra_error": pair.get("infra_error"),
        }
        pair_results.append(row)
        if pair.get("pair_verdict") in {"supports", "contributes"} and key:
            supporting_keys.append(key)

    set_result = await verify_claim_citation_set(
        claim_text, cites, model_key=model_key, user_id=user_id
    )
    if set_result.get("infra_error") and not infra:
        infra = str(set_result["infra_error"])

    set_verdict = set_result.get("set_verdict")
    return {
        "verdict": set_verdict,
        "set_verdict": set_verdict,
        "support_confidence": set_result.get("support_confidence"),
        "support_span_found": any(p.get("support_span_found") for p in pair_results),
        "verifier_provenance": VERIFIER_PROVENANCE,
        "infra_error": infra,
        "supported_by_any": set_verdict == "supported",
        "pair_results": pair_results,
        "supporting_reference_keys": supporting_keys,
        "evidence_sha256_list": set_result.get("evidence_sha256_list") or [],
        "temperature": VERIFIER_TEMPERATURE,
    }


async def verify_case_claims(
    claims: Sequence[Dict[str, Any]],
    references: Sequence[Dict[str, Any]],
    *,
    model_key: Optional[str] = None,
    user_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Run v2 verifier for each factual claim."""
    by_key: Dict[str, Dict[str, Any]] = {}
    for ref in annotate_evidence_serialization(list(references or [])):
        key = str(ref.get("reference_key") or "").strip()
        if key:
            by_key[key] = ref

    per_claim: List[Dict[str, Any]] = []
    infra: Optional[str] = None
    supporting_keys_global = set()

    for claim in claims or []:
        if not isinstance(claim, dict):
            continue
        text = str(claim.get("text") or "").strip()
        if not text:
            continue
        keys = [str(k).strip() for k in (claim.get("reference_keys") or []) if str(k).strip()]
        cites = [by_key[k] for k in keys if k in by_key]
        result = await verify_claim_against_evidence(
            text, cites, model_key=model_key, user_id=user_id
        )
        if result.get("infra_error") and not infra:
            infra = str(result["infra_error"])
        for key in result.get("supporting_reference_keys") or []:
            supporting_keys_global.add(str(key))
        # Public row: no claim text / evidence body
        per_claim.append(
            {
                "claim_index": len(per_claim),
                "bound_key_count": len(keys),
                "evidence_count": len(cites),
                "verdict": result.get("set_verdict") or result.get("verdict"),
                "set_verdict": result.get("set_verdict"),
                "support_confidence": result.get("support_confidence"),
                "support_span_found": result.get("support_span_found"),
                "supported_by_any": result.get("supported_by_any"),
                "pair_results": [
                    {
                        "reference_key": p.get("reference_key"),
                        "pair_verdict": p.get("pair_verdict"),
                        "evidence_sha256": p.get("evidence_sha256"),
                        "infra_error": p.get("infra_error"),
                    }
                    for p in (result.get("pair_results") or [])
                ],
                "supporting_reference_keys": list(result.get("supporting_reference_keys") or []),
                "evidence_sha256_list": list(result.get("evidence_sha256_list") or []),
                "infra_error": result.get("infra_error"),
            }
        )

    supported_claim_count = sum(1 for row in per_claim if row.get("supported_by_any") is True)
    return {
        "verifier_provenance": VERIFIER_PROVENANCE,
        "verifier_temperature": VERIFIER_TEMPERATURE,
        "infra_error": infra,
        "claim_verdicts": per_claim,
        "supported_claim_count": supported_claim_count,
        "factual_claim_count": len(per_claim),
        # Only pair-level supports|contributes keys — NEVER all keys of a supported set.
        "semantically_supporting_keys": sorted(supporting_keys_global),
    }


async def stability_recheck_claim(
    claim_text: str,
    citations: Sequence[Dict[str, Any]],
    *,
    model_key: Optional[str] = None,
    user_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Blind + reversed-order set rechecks. Inconsistency → treat as unsupported.
    """
    primary = await verify_claim_against_evidence(
        claim_text, citations, model_key=model_key, user_id=user_id
    )
    reversed_set = await verify_claim_citation_set(
        claim_text,
        citations,
        model_key=model_key,
        user_id=user_id,
        reverse_order=True,
        blind=True,
    )
    # Blind pairs on first citation only for cost control when multi.
    blind_pairs = []
    infra = primary.get("infra_error") or reversed_set.get("infra_error")
    for cite in list(citations or [])[:3]:
        if not isinstance(cite, dict):
            continue
        bp = await verify_claim_citation_pair(
            claim_text, cite, model_key=model_key, user_id=user_id, blind=True
        )
        if bp.get("infra_error") and not infra:
            infra = bp.get("infra_error")
        blind_pairs.append(
            {
                "reference_key": cite.get("reference_key"),
                "pair_verdict": bp.get("pair_verdict"),
                "infra_error": bp.get("infra_error"),
            }
        )

    primary_set = primary.get("set_verdict") or primary.get("verdict")
    rev_set = reversed_set.get("set_verdict")
    consistent = (
        primary_set is not None
        and rev_set is not None
        and primary_set == rev_set
        and not infra
    )
    effective = primary_set if consistent else "insufficient"
    if primary_set == "contradicted" or rev_set == "contradicted":
        # Keep contradicted if either run says so and no infra.
        if not infra and (primary_set == "contradicted" or rev_set == "contradicted"):
            if primary_set != rev_set:
                effective = "insufficient"  # disagreement → unsupported, not cherry-pick
            else:
                effective = "contradicted"

    return {
        "verifier_provenance": VERIFIER_PROVENANCE_BLIND,
        "primary_set_verdict": primary_set,
        "reversed_set_verdict": rev_set,
        "consistent": consistent,
        "effective_set_verdict": effective,
        "infra_error": infra,
        "blind_pair_sample": blind_pairs,
        "primary_supporting_keys": list(primary.get("supporting_reference_keys") or []),
    }
