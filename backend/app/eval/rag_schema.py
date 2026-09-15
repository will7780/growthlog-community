"""
Central RAG case / qrels contract.

Stages:
  - candidate: export/draft tooling (pending/draft/example allowed; legacy ok)
  - reviewed: promotion input (reviewed/approved only; no example; legacy ok)
  - frozen: formal eval sets (full fields; qrels only; no example; no legacy)

All validate/promote/eval_rag/release-gate RAG paths must use this module.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Optional, Tuple

ALLOWED_SPLITS = frozenset({"dev", "regression"})
ALLOWED_RELEVANCE = frozenset({1, 2, 3})
ALLOWED_CANDIDATE_STATUS = frozenset(
    {"pending", "reviewed", "approved", "example", "draft"}
)
ALLOWED_REVIEWED_STATUS = frozenset({"reviewed", "approved"})
# Align with rank_fusion.result_key: "{source_type}:{chunk|source|entry}:{id}"
REFERENCE_KEY_RE = re.compile(
    r"^(?P<source_type>[a-z][a-z0-9_]{0,62}):(?P<kind>chunk|source|entry):(?P<id>\d+)$"
)

STAGE_CANDIDATE = "candidate"
STAGE_REVIEWED = "reviewed"
STAGE_FROZEN = "frozen"
VALID_STAGES = frozenset({STAGE_CANDIDATE, STAGE_REVIEWED, STAGE_FROZEN})

FORMAL_REQUIRED_FIELDS = (
    "case_id",
    "user_id",
    "query",
    "query_type",
    "tags",
    "split",
    "expected_answerable",
    "qrels",
    "difficulty",
    "manual_label_status",
)

LEGACY_FIELDS = ("relevant_entry_ids", "relevant_chunk_ids", "relevant_keys")

LEGACY_ENTRY_KEY_TEMPLATES = (
    "entry:source:{id}",
    "entry:entry:{id}",
)
LEGACY_CHUNK_KEY_TEMPLATES = (
    "attachment_chunk:chunk:{id}",
)

# Stable error codes for promote / gate surfaces.
E_EXAMPLE_NOT_PROMOTABLE = "E_EXAMPLE_NOT_PROMOTABLE"
E_PENDING_NOT_PROMOTABLE = "E_PENDING_NOT_PROMOTABLE"
E_DRAFT_NOT_PROMOTABLE = "E_DRAFT_NOT_PROMOTABLE"
E_LEGACY_IN_FROZEN = "E_LEGACY_IN_FROZEN"
E_SCHEMA_INVALID = "E_SCHEMA_INVALID"


class CaseValidationError(ValueError):
    def __init__(self, message: str, *, code: str = E_SCHEMA_INVALID):
        super().__init__(message)
        self.code = code


def normalize_query_for_id(query: str) -> str:
    return " ".join(str(query or "").strip().lower().split())


def stable_legacy_case_id(user_id: int, query: str) -> str:
    """Deterministic cross-process case_id (never use Python hash())."""
    digest = hashlib.sha256(
        f"{int(user_id)}\n{normalize_query_for_id(query)}".encode("utf-8")
    ).hexdigest()[:16]
    return f"legacy-{int(user_id)}-{digest}"


def parse_reference_key(key: str) -> Dict[str, str]:
    text = str(key or "").strip()
    m = REFERENCE_KEY_RE.fullmatch(text)
    if not m:
        raise CaseValidationError(f"invalid reference_key: {key!r}")
    return m.groupdict()


def legacy_keys_from_case(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Expand legacy relevant_* fields into synthetic qrels (relevance=2)."""
    qrels: List[Dict[str, Any]] = []
    seen: set[str] = set()

    for key in raw.get("relevant_keys") or []:
        text = str(key).strip()
        if not text or text in seen:
            continue
        parsed = parse_reference_key(text)
        seen.add(text)
        qrels.append(
            {
                "reference_key": text,
                "relevance": 2,
                "source_type": parsed["source_type"],
            }
        )

    for entry_id in raw.get("relevant_entry_ids") or []:
        eid = int(entry_id)
        for tmpl in LEGACY_ENTRY_KEY_TEMPLATES:
            key = tmpl.format(id=eid)
            if key in seen:
                continue
            seen.add(key)
            qrels.append(
                {
                    "reference_key": key,
                    "relevance": 2,
                    "source_type": "entry",
                }
            )

    for chunk_id in raw.get("relevant_chunk_ids") or []:
        cid = int(chunk_id)
        for tmpl in LEGACY_CHUNK_KEY_TEMPLATES:
            key = tmpl.format(id=cid)
            if key in seen:
                continue
            seen.add(key)
            qrels.append(
                {
                    "reference_key": key,
                    "relevance": 2,
                    "source_type": "attachment_chunk",
                }
            )
    return qrels


def has_legacy_label_fields(raw: Dict[str, Any]) -> bool:
    for field in LEGACY_FIELDS:
        value = raw.get(field)
        if isinstance(value, list) and value:
            return True
    return False


def _coerce_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    raise CaseValidationError(f"{field} must be a boolean")


def validate_qrel(item: Any, *, index: int) -> Dict[str, Any]:
    if not isinstance(item, dict):
        raise CaseValidationError(f"qrels[{index}] must be an object")
    key = str(item.get("reference_key") or "").strip()
    parsed = parse_reference_key(key)
    rel = item.get("relevance")
    if isinstance(rel, bool) or not isinstance(rel, int) or rel not in ALLOWED_RELEVANCE:
        raise CaseValidationError(
            f"qrels[{index}].relevance must be one of {sorted(ALLOWED_RELEVANCE)}"
        )
    source_type = str(item.get("source_type") or "").strip()
    if not source_type:
        raise CaseValidationError(f"qrels[{index}].source_type is required")
    if source_type != parsed["source_type"]:
        raise CaseValidationError(
            f"qrels[{index}].source_type {source_type!r} must match "
            f"reference_key type {parsed['source_type']!r}"
        )
    return {
        "reference_key": key,
        "relevance": int(rel),
        "source_type": source_type,
    }


def _dedupe_qrels(qrels: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    best: Dict[str, Dict[str, Any]] = {}
    for q in qrels:
        prev = best.get(q["reference_key"])
        if prev is None or q["relevance"] > prev["relevance"]:
            best[q["reference_key"]] = q
    return list(best.values())


def normalize_case(
    raw: Dict[str, Any],
    *,
    stage: str = STAGE_CANDIDATE,
    allow_legacy: Optional[bool] = None,
    require_formal: Optional[bool] = None,
    line_no: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Validate and normalize one case for the given stage.

    require_formal=True is accepted as an alias for stage=frozen (compat).
    """
    if require_formal is True:
        stage = STAGE_FROZEN
    if stage not in VALID_STAGES:
        raise CaseValidationError(f"unknown stage: {stage}")

    if allow_legacy is None:
        allow_legacy = stage in {STAGE_CANDIDATE, STAGE_REVIEWED}

    prefix = f"line {line_no}: " if line_no is not None else ""

    def fail(msg: str, code: str = E_SCHEMA_INVALID) -> None:
        raise CaseValidationError(prefix + msg, code=code)

    if not isinstance(raw, dict):
        fail("case must be a JSON object")

    example = bool(raw.get("example", False))
    if example and stage in {STAGE_REVIEWED, STAGE_FROZEN}:
        fail("example=true is not allowed at reviewed/frozen stage", E_EXAMPLE_NOT_PROMOTABLE)

    case_id = str(raw.get("case_id") or "").strip()
    if stage in {STAGE_REVIEWED, STAGE_FROZEN} and not case_id:
        fail("case_id is required")

    if "user_id" not in raw:
        fail("user_id is required")
    try:
        user_id = int(raw["user_id"])
    except (TypeError, ValueError):
        fail("user_id must be an integer")

    query = str(raw.get("query") or "").strip()
    if not query:
        fail("query is required")

    if not case_id:
        case_id = stable_legacy_case_id(user_id, query)

    split = raw.get("split")
    if split is not None and str(split).strip() != "":
        split = str(split).strip()
        if split not in ALLOWED_SPLITS:
            fail(f"split must be one of {sorted(ALLOWED_SPLITS)}")
    else:
        split = None

    tags = raw.get("tags")
    if tags is None:
        tags = []
    if not isinstance(tags, list) or any(not isinstance(t, str) for t in tags):
        fail("tags must be a list of strings")

    difficulty = raw.get("difficulty")
    if difficulty is not None and str(difficulty).strip() != "":
        difficulty = str(difficulty).strip()
    else:
        difficulty = None

    status = raw.get("manual_label_status")
    if status is not None and str(status).strip() != "":
        status = str(status).strip()
    else:
        status = None

    if stage == STAGE_CANDIDATE:
        if status is not None and status not in ALLOWED_CANDIDATE_STATUS:
            fail(f"manual_label_status must be one of {sorted(ALLOWED_CANDIDATE_STATUS)}")
    else:
        if status is None:
            fail("manual_label_status is required")
        if status in {"pending"}:
            fail("pending status cannot be promoted/frozen", E_PENDING_NOT_PROMOTABLE)
        if status in {"draft"}:
            fail("draft status cannot be promoted/frozen", E_DRAFT_NOT_PROMOTABLE)
        if status == "example":
            fail("example status cannot be promoted/frozen", E_EXAMPLE_NOT_PROMOTABLE)
        if status not in ALLOWED_REVIEWED_STATUS:
            fail(f"manual_label_status must be one of {sorted(ALLOWED_REVIEWED_STATUS)}")

    used_legacy = False
    raw_qrels = raw.get("qrels")
    if raw_qrels is None:
        if has_legacy_label_fields(raw):
            if not allow_legacy:
                fail(
                    "legacy relevant_* fields are not allowed without --allow-legacy",
                    E_LEGACY_IN_FROZEN,
                )
            used_legacy = True
            qrels = legacy_keys_from_case(raw)
        else:
            qrels = []
    else:
        if not isinstance(raw_qrels, list):
            fail("qrels must be a list")
        qrels = _dedupe_qrels(
            [validate_qrel(item, index=i) for i, item in enumerate(raw_qrels)]
        )
        if has_legacy_label_fields(raw) and stage == STAGE_FROZEN and not allow_legacy:
            fail(
                "frozen sets must use qrels only (legacy relevant_* present)",
                E_LEGACY_IN_FROZEN,
            )

    if stage == STAGE_FROZEN and used_legacy and not allow_legacy:
        fail("frozen sets must use qrels (legacy relevant_* forbidden)", E_LEGACY_IN_FROZEN)

    no_answer = bool(raw.get("no_answer", False))
    if "expected_answerable" in raw and raw["expected_answerable"] is not None:
        expected_answerable = _coerce_bool(raw["expected_answerable"], "expected_answerable")
    elif stage in {STAGE_REVIEWED, STAGE_FROZEN}:
        fail("expected_answerable is required")
    else:
        expected_answerable = not no_answer and len(qrels) > 0

    if expected_answerable and not qrels:
        fail("expected_answerable=true requires non-empty qrels")
    if not expected_answerable and qrels:
        fail("expected_answerable=false must not include qrels")
    if no_answer and expected_answerable:
        fail("no_answer=true conflicts with expected_answerable=true")

    query_type = str(raw.get("query_type") or "").strip() or None

    if stage in {STAGE_REVIEWED, STAGE_FROZEN}:
        if not query_type:
            fail("query_type is required")
        if "tags" not in raw:
            fail("tags is required")
        if split is None:
            fail("split is required")
        if difficulty is None:
            fail("difficulty is required")
        if raw_qrels is None and not used_legacy:
            # Explicit empty qrels list is ok for no-answer; missing key with no
            # legacy is only ok when expected_answerable=false (already checked).
            if expected_answerable:
                fail("qrels is required")
        if stage == STAGE_FROZEN and raw_qrels is None and used_legacy and allow_legacy:
            # Normalized output will carry qrels; input may be legacy under flag.
            pass
        if stage == STAGE_FROZEN and not allow_legacy and raw_qrels is None:
            fail("qrels is required for frozen sets")

    rankings = raw.get("rankings") or {}
    if rankings is not None and not isinstance(rankings, dict):
        fail("rankings must be an object when present")

    coverage_needed = sorted({str(q["source_type"]) for q in qrels if q.get("source_type")})

    return {
        "case_id": case_id,
        "user_id": user_id,
        "query": query,
        "query_type": query_type,
        "tags": list(tags),
        "split": split,
        "expected_answerable": expected_answerable,
        "qrels": qrels,
        "difficulty": difficulty,
        "manual_label_status": status,
        "example": example,
        "no_answer": no_answer or (not expected_answerable),
        "rankings": rankings if isinstance(rankings, dict) else {},
        "coverage_needed": coverage_needed,
        "used_legacy": used_legacy,
        "stage": stage,
    }


def validate_case(
    raw: Dict[str, Any],
    *,
    stage: str = STAGE_CANDIDATE,
    allow_legacy: Optional[bool] = None,
    require_formal: bool = False,
    line_no: Optional[int] = None,
) -> Tuple[List[str], List[str], Optional[Dict[str, Any]]]:
    try:
        case = normalize_case(
            raw,
            stage=STAGE_FROZEN if require_formal else stage,
            allow_legacy=allow_legacy,
            line_no=line_no,
        )
        warnings: List[str] = []
        if case.get("used_legacy"):
            warnings.append("legacy relevant_* expanded to qrels")
        return [], warnings, case
    except CaseValidationError as exc:
        return [str(exc)], [], None


def case_to_frozen_record(case: Dict[str, Any]) -> Dict[str, Any]:
    """Serialize a normalized case for formal JSONL (no legacy / notes)."""
    return {
        "case_id": case["case_id"],
        "user_id": case["user_id"],
        "query": case["query"],
        "query_type": case["query_type"],
        "tags": list(case.get("tags") or []),
        "split": case["split"],
        "expected_answerable": bool(case["expected_answerable"]),
        "qrels": list(case.get("qrels") or []),
        "difficulty": case["difficulty"],
        "manual_label_status": case["manual_label_status"],
        "no_answer": bool(case.get("no_answer")),
    }


def graded_relevance_map(case: Dict[str, Any]) -> Dict[str, int]:
    return {
        str(q["reference_key"]): int(q["relevance"])
        for q in case.get("qrels") or []
    }


def binary_relevant_keys(case: Dict[str, Any]) -> set[str]:
    return set(graded_relevance_map(case))
