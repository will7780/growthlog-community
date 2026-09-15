"""
R5.1.2 / R5.2.1 auditable Prompt gold review artifact.

Final gold is ONLY include=true rows in a private, non-auto-recomputed
decision artifact. Heuristics may discover candidates but never decide truth.
accepted_included_count in the artifact is the sole expected include count —
runtime must not hardcode a fixed include total.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set

REVIEW_DEFINITION_VERSION = "r5_prompt_gold_v2_semantic_20"
REVIEWER_PROVENANCE = "cursor_assisted"

ALLOWED_CATEGORIES = frozenset(
    {
        "display_topic",
        "body_topic",
        "excluded_incidental",
        "excluded_not_prompt_topic",
    }
)

E_ARTIFACT_MISSING = "E_ARTIFACT_MISSING"
E_ARTIFACT_SCHEMA = "E_ARTIFACT_SCHEMA"
E_ARTIFACT_SHA = "E_ARTIFACT_SHA"
E_DECISION_MISSING = "E_DECISION_MISSING"
E_DECISION_DUPLICATE = "E_DECISION_DUPLICATE"
E_DECISION_UNKNOWN = "E_DECISION_UNKNOWN"
E_DECISION_EXTRA = "E_DECISION_EXTRA"
E_CANDIDATE_DRIFT = "E_CANDIDATE_DRIFT"
E_FINGERPRINT_DRIFT = "E_FINGERPRINT_DRIFT"
E_GOLD_COUNT = "E_GOLD_COUNT"
E_ACCEPTED_COUNT = "E_ACCEPTED_COUNT"


class ReviewArtifactError(ValueError):
    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


def content_fingerprint(title: str, content: str) -> str:
    """Stable fingerprint of entry text without retaining the text."""
    payload = f"{title or ''}\n---\n{content or ''}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def id_hash_for_entry(entry_id: int) -> str:
    return hashlib.sha256(f"entry:{int(entry_id)}".encode("utf-8")).hexdigest()[:16]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_review_artifact(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        raise ReviewArtifactError(f"missing artifact: {path}", code=E_ARTIFACT_MISSING)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ReviewArtifactError(f"invalid JSON: {exc}", code=E_ARTIFACT_SCHEMA) from exc
    if not isinstance(data, dict):
        raise ReviewArtifactError("artifact root must be object", code=E_ARTIFACT_SCHEMA)
    return data


def validate_review_artifact(
    artifact: Mapping[str, Any],
    *,
    candidate_rows: Sequence[Mapping[str, Any]],
    expected_sha256: Optional[str] = None,
    artifact_bytes: Optional[bytes] = None,
) -> Dict[str, Any]:
    """
    Hard-fail validation.

    candidate_rows items must include: id_hash, reference_key, content_fingerprint
    (and optionally entry_id).

    Expected include count comes ONLY from artifact.accepted_included_count.
    """
    if artifact.get("review_definition_version") != REVIEW_DEFINITION_VERSION:
        raise ReviewArtifactError(
            "review_definition_version mismatch",
            code=E_ARTIFACT_SCHEMA,
        )
    if artifact.get("reviewer_provenance") != REVIEWER_PROVENANCE:
        raise ReviewArtifactError(
            "reviewer_provenance must be cursor_assisted",
            code=E_ARTIFACT_SCHEMA,
        )
    if "accepted_included_count" not in artifact:
        raise ReviewArtifactError(
            "accepted_included_count required",
            code=E_ARTIFACT_SCHEMA,
        )
    try:
        accepted = int(artifact["accepted_included_count"])
    except (TypeError, ValueError) as exc:
        raise ReviewArtifactError(
            "accepted_included_count must be int",
            code=E_ARTIFACT_SCHEMA,
        ) from exc
    if accepted < 1:
        raise ReviewArtifactError(
            "accepted_included_count must be >= 1",
            code=E_ARTIFACT_SCHEMA,
        )

    decisions = artifact.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        raise ReviewArtifactError("decisions must be non-empty list", code=E_ARTIFACT_SCHEMA)

    if expected_sha256 is not None:
        if not artifact_bytes:
            raise ReviewArtifactError("artifact_bytes required for SHA check", code=E_ARTIFACT_SHA)
        actual = hashlib.sha256(artifact_bytes).hexdigest()
        if actual.lower() != expected_sha256.lower():
            raise ReviewArtifactError("artifact SHA mismatch", code=E_ARTIFACT_SHA)

    cand_by_hash: Dict[str, Mapping[str, Any]] = {}
    for row in candidate_rows:
        h = str(row.get("id_hash") or "").strip()
        if not h:
            raise ReviewArtifactError("candidate missing id_hash", code=E_CANDIDATE_DRIFT)
        if h in cand_by_hash:
            raise ReviewArtifactError(f"duplicate candidate {h}", code=E_CANDIDATE_DRIFT)
        cand_by_hash[h] = row
    cand_hashes = set(cand_by_hash.keys())

    seen: Set[str] = set()
    normalized: List[Dict[str, Any]] = []
    for idx, row in enumerate(decisions):
        if not isinstance(row, dict):
            raise ReviewArtifactError(f"decisions[{idx}] not object", code=E_ARTIFACT_SCHEMA)
        h = str(row.get("id_hash") or "").strip()
        if not h:
            raise ReviewArtifactError(f"decisions[{idx}] missing id_hash", code=E_ARTIFACT_SCHEMA)
        if h in seen:
            raise ReviewArtifactError(f"duplicate decision for {h}", code=E_DECISION_DUPLICATE)
        seen.add(h)
        if h not in cand_hashes:
            raise ReviewArtifactError(f"unknown decision id_hash {h}", code=E_DECISION_UNKNOWN)
        if "include" not in row or not isinstance(row.get("include"), bool):
            raise ReviewArtifactError(
                f"decisions[{idx}].include must be bool",
                code=E_ARTIFACT_SCHEMA,
            )
        cat = str(row.get("category") or "").strip()
        if cat not in ALLOWED_CATEGORIES:
            raise ReviewArtifactError(
                f"decisions[{idx}].category invalid",
                code=E_ARTIFACT_SCHEMA,
            )
        rationale = str(row.get("rationale_code") or "").strip()
        if not rationale:
            raise ReviewArtifactError(
                f"decisions[{idx}].rationale_code required",
                code=E_ARTIFACT_SCHEMA,
            )
        ref_key = str(row.get("reference_key") or "").strip()
        cand = cand_by_hash[h]
        if ref_key and ref_key != str(cand.get("reference_key") or ""):
            raise ReviewArtifactError(
                f"reference_key drift for {h}",
                code=E_CANDIDATE_DRIFT,
            )
        fp = str(row.get("content_fingerprint") or "").strip()
        cand_fp = str(cand.get("content_fingerprint") or "").strip()
        if fp and cand_fp and fp != cand_fp:
            raise ReviewArtifactError(
                f"content fingerprint drift for {h}",
                code=E_FINGERPRINT_DRIFT,
            )
        if str(row.get("reviewer_provenance") or REVIEWER_PROVENANCE) != REVIEWER_PROVENANCE:
            raise ReviewArtifactError(
                f"decisions[{idx}] provenance invalid",
                code=E_ARTIFACT_SCHEMA,
            )
        if str(row.get("review_definition_version") or REVIEW_DEFINITION_VERSION) != REVIEW_DEFINITION_VERSION:
            raise ReviewArtifactError(
                f"decisions[{idx}] definition version invalid",
                code=E_ARTIFACT_SCHEMA,
            )
        for banned in ("content", "title", "body", "snippet", "ocr_text", "query"):
            if banned in row and row.get(banned) not in (None, "", "<redacted>"):
                raise ReviewArtifactError(
                    f"decisions[{idx}] must not contain {banned}",
                    code=E_ARTIFACT_SCHEMA,
                )
        normalized.append(
            {
                "id_hash": h,
                "reference_key": str(cand.get("reference_key") or ref_key),
                "include": bool(row["include"]),
                "category": cat,
                "rationale_code": rationale,
                "reviewer_provenance": REVIEWER_PROVENANCE,
                "review_definition_version": REVIEW_DEFINITION_VERSION,
                "content_fingerprint": cand_fp or fp,
                "entry_id": cand.get("entry_id"),
            }
        )

    missing = sorted(cand_hashes - seen)
    if missing:
        raise ReviewArtifactError(
            f"missing decisions for {len(missing)} candidates",
            code=E_DECISION_MISSING,
        )
    extra = sorted(seen - cand_hashes)
    if extra:
        raise ReviewArtifactError(
            f"extra decisions not in candidates: {len(extra)}",
            code=E_DECISION_EXTRA,
        )

    included = [d for d in normalized if d["include"]]
    if len(included) != accepted:
        raise ReviewArtifactError(
            f"include=true count {len(included)} != accepted_included_count {accepted}",
            code=E_ACCEPTED_COUNT,
        )

    return {
        "ok": True,
        "candidate_count": len(cand_hashes),
        "decision_count": len(normalized),
        "included_count": len(included),
        "accepted_included_count": accepted,
        "excluded_count": len(normalized) - len(included),
        "included": included,
        "all_decisions": normalized,
        "review_definition_version": REVIEW_DEFINITION_VERSION,
    }


def gold_from_validated(validated: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Build final gold list solely from artifact include=true rows."""
    accepted = int(validated.get("accepted_included_count") or 0)
    out: List[Dict[str, Any]] = []
    for row in validated.get("included") or []:
        eid = row.get("entry_id")
        if eid is None:
            raise ReviewArtifactError("included row missing entry_id", code=E_ARTIFACT_SCHEMA)
        out.append(
            {
                "entry_id": int(eid),
                "id_hash": row["id_hash"],
                "category": row["category"],
                "rationale_code": row["rationale_code"],
                "reference_key": row["reference_key"],
                "provenance": REVIEWER_PROVENANCE,
                "review_definition_version": REVIEW_DEFINITION_VERSION,
                "content_fingerprint": row.get("content_fingerprint"),
            }
        )
    if len(out) != accepted:
        raise ReviewArtifactError(
            f"gold size {len(out)} != accepted_included_count {accepted}",
            code=E_ACCEPTED_COUNT,
        )
    return out
