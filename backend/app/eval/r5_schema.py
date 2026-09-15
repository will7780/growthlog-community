"""
Phase R5 eval contracts: intent, LLM relevance judge, matches vs used_references.

LLM still decides relevance / found-or-not. Server owns candidate completeness,
allowed-key checks, batch coverage, and failure semantics.

This module is contract-only for R5.1 — not wired into production AI routes.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union
from urllib.parse import urlparse

from app.eval.rag_schema import REFERENCE_KEY_RE, CaseValidationError, parse_reference_key

INTENT_LOOKUP_ONE = "lookup_one"
INTENT_LOOKUP_ALL = "lookup_all"
INTENT_ANSWER = "answer"
ALLOWED_INTENTS = frozenset({INTENT_LOOKUP_ONE, INTENT_LOOKUP_ALL, INTENT_ANSWER})
LOOKUP_INTENTS = frozenset({INTENT_LOOKUP_ONE, INTENT_LOOKUP_ALL})

REASON_CODES = frozenset(
    {
        # Generic semantic codes (preferred)
        "direct_topic",
        "semantic_equivalent",
        "incidental",
        "unrelated",
        "insufficient_evidence",
        # Legacy aliases still accepted for schema tolerance
        "relevant",
        "not_relevant",
        "duplicate",
        "unknown_key",
        "provider_error",
        "invalid_json",
        "missing_judgment",
    }
)

E_JUDGE_UNKNOWN_KEY = "E_JUDGE_UNKNOWN_KEY"
E_JUDGE_DUPLICATE_KEY = "E_JUDGE_DUPLICATE_KEY"
E_JUDGE_MISSING_KEY = "E_JUDGE_MISSING_KEY"
E_JUDGE_INVALID_JSON = "E_JUDGE_INVALID_JSON"
E_JUDGE_PROVIDER_FAILED = "E_JUDGE_PROVIDER_FAILED"
E_JUDGE_COVERAGE = "E_JUDGE_COVERAGE"
E_JUDGE_SCHEMA = "E_JUDGE_SCHEMA"
E_JUDGE_FOUND_INVALID = "E_JUDGE_FOUND_INVALID"
E_JUDGE_FOUND_INCONSISTENT = "E_JUDGE_FOUND_INCONSISTENT"
E_INTENT_INVALID = "E_INTENT_INVALID"
E_PRIVATE_PATH = "E_PRIVATE_PATH"
E_GOLD_CORPUS_MISMATCH = "E_GOLD_CORPUS_MISMATCH"

JUDGE_TEMPERATURE = 0.0
JUDGE_STABILITY_REPEATS = 3

PRIVATE_EVAL_RELDIR = "test_artifacts/evals/r5"
SENSITIVE_REPORT_KEYS = frozenset(
    {
        "content",
        "snippet",
        "ocr_text",
        "ocr_full_text",
        "body",
        "title",
        "answer",
        "query",
        "hit_window",
        "database_url",
        "api_key",
        "authorization",
        "password",
        "secret",
        "llm_prompt",
        "system_prompt",
        "user_prompt",
    }
)

ABSTENTION_MARKERS = ("没有找到", "未找到", "没有相关", "未能找到")


class JudgeValidationError(ValueError):
    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


def stable_case_id(prefix: str, *parts: Any) -> str:
    digest = hashlib.sha256(
        "\n".join(str(p) for p in parts).encode("utf-8")
    ).hexdigest()[:16]
    return f"{prefix}-{digest}"


def entry_id_from_reference_key(key: str) -> Optional[int]:
    try:
        parsed = parse_reference_key(key)
    except CaseValidationError:
        return None
    if parsed["source_type"] != "entry":
        return None
    if parsed["kind"] not in {"source", "entry"}:
        return None
    return int(parsed["id"])


def normalize_entry_gold_key(entry_id: int) -> str:
    return f"entry:source:{int(entry_id)}"


def expand_entry_gold_aliases(entry_id: int) -> List[str]:
    eid = int(entry_id)
    return [f"entry:source:{eid}", f"entry:entry:{eid}"]


def graded_from_entry_ids(entry_ids: Iterable[int]) -> Dict[str, int]:
    graded: Dict[str, int] = {}
    for eid in entry_ids:
        graded[normalize_entry_gold_key(int(eid))] = 2
    return graded


def project_refs_to_entry_keys(refs: Sequence[Mapping[str, Any]]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for ref in refs:
        if not isinstance(ref, Mapping):
            continue
        eid = ref.get("entry_id")
        if eid is not None:
            projected = normalize_entry_gold_key(int(eid))
        else:
            key = str(ref.get("reference_key") or "").strip()
            if not key:
                from app.services.rank_fusion import result_key

                key = result_key(dict(ref))
            try:
                parsed = parse_reference_key(key)
            except CaseValidationError:
                continue
            if parsed["source_type"] == "entry" and parsed["kind"] in {"source", "entry"}:
                projected = normalize_entry_gold_key(int(parsed["id"]))
            else:
                projected = key
        if projected in seen:
            continue
        seen.add(projected)
        out.append(projected)
    return out


def project_ranked_keys_safe(ranked_keys: Sequence[str]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for raw in ranked_keys:
        key = str(raw or "").strip()
        if not key:
            continue
        try:
            parsed = parse_reference_key(key)
        except CaseValidationError:
            continue
        if parsed["source_type"] == "entry" and parsed["kind"] in {"source", "entry"}:
            projected = normalize_entry_gold_key(int(parsed["id"]))
        else:
            projected = key
        if projected in seen:
            continue
        seen.add(projected)
        out.append(projected)
    return out


def validate_intent(raw: Any) -> str:
    intent = str(raw or "").strip()
    if intent not in ALLOWED_INTENTS:
        raise JudgeValidationError(
            f"invalid intent: {raw!r}; expected one of {sorted(ALLOWED_INTENTS)}",
            code=E_INTENT_INVALID,
        )
    return intent


def require_found_boolean(*, intent: str, payload: Mapping[str, Any]) -> Optional[bool]:
    """
    For lookup_one / lookup_all: found must be present and a JSON boolean.
    Missing, string \"false\", numbers, etc. are hard contract errors.
    Never derive found from match_count.
    """
    intent_n = validate_intent(intent)
    if intent_n not in LOOKUP_INTENTS:
        raw = payload.get("found")
        return raw if isinstance(raw, bool) else None
    if "found" not in payload:
        raise JudgeValidationError(
            "lookup intent requires explicit boolean found",
            code=E_JUDGE_FOUND_INVALID,
        )
    found = payload["found"]
    if not isinstance(found, bool):
        raise JudgeValidationError(
            f"found must be JSON boolean, got {type(found).__name__}:{found!r}",
            code=E_JUDGE_FOUND_INVALID,
        )
    return found


def _parse_judgment_row(row: Any, *, index: int) -> Dict[str, Any]:
    if not isinstance(row, dict):
        raise JudgeValidationError(
            f"judgments[{index}] must be an object",
            code=E_JUDGE_SCHEMA,
        )
    key = str(row.get("reference_key") or "").strip()
    if not REFERENCE_KEY_RE.fullmatch(key):
        raise JudgeValidationError(
            f"judgments[{index}].reference_key invalid: {key!r}",
            code=E_JUDGE_SCHEMA,
        )
    if "relevant" not in row:
        raise JudgeValidationError(
            f"judgments[{index}].relevant is required",
            code=E_JUDGE_SCHEMA,
        )
    relevant = row.get("relevant")
    if not isinstance(relevant, bool):
        raise JudgeValidationError(
            f"judgments[{index}].relevant must be bool",
            code=E_JUDGE_SCHEMA,
        )
    reason = str(row.get("reason_code") or "").strip()
    if reason and reason not in REASON_CODES:
        raise JudgeValidationError(
            f"judgments[{index}].reason_code invalid: {reason!r}",
            code=E_JUDGE_SCHEMA,
        )
    if not reason:
        reason = "relevant" if relevant else "not_relevant"
    return {
        "reference_key": key,
        "relevant": relevant,
        "reason_code": reason,
    }


def parse_judge_payload(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise JudgeValidationError("empty judge payload", code=E_JUDGE_INVALID_JSON)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise JudgeValidationError(
                f"invalid judge JSON: {exc}",
                code=E_JUDGE_INVALID_JSON,
            ) from exc
    elif isinstance(raw, dict):
        payload = raw
    else:
        raise JudgeValidationError(
            f"judge payload must be str or dict, got {type(raw).__name__}",
            code=E_JUDGE_SCHEMA,
        )
    if not isinstance(payload, dict):
        raise JudgeValidationError("judge payload root must be object", code=E_JUDGE_SCHEMA)
    intent = validate_intent(payload.get("intent") or INTENT_ANSWER)
    judgments_raw = payload.get("judgments")
    if judgments_raw is None:
        judgments_raw = payload.get("candidates")
    if not isinstance(judgments_raw, list):
        raise JudgeValidationError("judgments must be a list", code=E_JUDGE_SCHEMA)
    judgments = [_parse_judgment_row(row, index=i) for i, row in enumerate(judgments_raw)]
    found = require_found_boolean(intent=intent, payload=payload)
    return {
        "intent": intent,
        "judgments": judgments,
        "found": found,
        "found_present": "found" in payload,
        "notes": payload.get("notes"),
        "_raw_payload": payload,
    }


def validate_judge_coverage(
    *,
    intent: str,
    candidate_keys: Sequence[str],
    judgments: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    intent_n = validate_intent(intent)
    candidates = [str(k).strip() for k in candidate_keys if str(k).strip()]
    candidate_set = set(candidates)
    if len(candidates) != len(candidate_set):
        raise JudgeValidationError(
            "candidate_keys contain duplicates",
            code=E_JUDGE_DUPLICATE_KEY,
        )

    seen: set[str] = set()
    ordered: List[Dict[str, Any]] = []
    for row in judgments:
        key = str(row.get("reference_key") or "").strip()
        if key in seen:
            raise JudgeValidationError(
                f"duplicate judgment for {key}",
                code=E_JUDGE_DUPLICATE_KEY,
            )
        seen.add(key)
        if key not in candidate_set:
            raise JudgeValidationError(
                f"unknown reference_key not in candidates: {key}",
                code=E_JUDGE_UNKNOWN_KEY,
            )
        ordered.append(
            {
                "reference_key": key,
                "relevant": bool(row.get("relevant")),
                "reason_code": str(row.get("reason_code") or ""),
            }
        )

    candidate_count = len(candidates)
    judged_count = len(ordered)
    missing = sorted(candidate_set - seen)

    if intent_n in LOOKUP_INTENTS:
        # R5.4.3: lookup_one and lookup_all both require one judgment per candidate.
        if missing:
            raise JudgeValidationError(
                f"{intent_n} missing judgments for {len(missing)} keys",
                code=E_JUDGE_MISSING_KEY,
            )
        if candidate_count != judged_count:
            raise JudgeValidationError(
                f"{intent_n} coverage failed: candidate_count={candidate_count} "
                f"judged_count={judged_count}",
                code=E_JUDGE_COVERAGE,
            )

    matches = [r["reference_key"] for r in ordered if r["relevant"]]
    return {
        "ok": True,
        "intent": intent_n,
        "candidate_count": candidate_count,
        "judged_count": judged_count,
        "match_count": len(matches),
        "matches": matches,
        "missing_keys": missing,
        "coverage_ok": (intent_n not in LOOKUP_INTENTS)
        or (candidate_count == judged_count and not missing),
    }


def provider_failure_result(*, error_type: str, detail: str = "") -> Dict[str, Any]:
    return {
        "ok": False,
        "error_code": E_JUDGE_PROVIDER_FAILED,
        "error_type": error_type,
        "detail": detail[:200],
        "candidate_count": None,
        "judged_count": None,
        "match_count": None,
        "matches": None,
        "found": None,
        "abstained": None,
        "must_not_treat_as_zero_hits": True,
    }


def judge_failure_from_exception(exc: BaseException) -> Dict[str, Any]:
    if isinstance(exc, JudgeValidationError):
        return {
            "ok": False,
            "error_code": exc.code,
            "error_type": type(exc).__name__,
            "detail": str(exc)[:200],
            "candidate_count": None,
            "judged_count": None,
            "match_count": None,
            "matches": None,
            "found": None,
            "must_not_treat_as_zero_hits": True,
        }
    return provider_failure_result(error_type=type(exc).__name__, detail=str(exc))


def apply_validated_judge(
    *,
    intent: str,
    candidate_keys: Sequence[str],
    raw_payload: Any,
) -> Dict[str, Any]:
    """Parse + coverage. found is LLM-owned; never inferred from match_count."""
    parsed = parse_judge_payload(raw_payload)
    intent_n = validate_intent(intent or parsed.get("intent"))
    # Re-validate found against the caller's intent (may differ from payload intent).
    found = require_found_boolean(intent=intent_n, payload=parsed.get("_raw_payload") or {})
    coverage = validate_judge_coverage(
        intent=intent_n,
        candidate_keys=candidate_keys,
        judgments=parsed["judgments"],
    )
    return {
        "ok": True,
        "intent": intent_n,
        "temperature": JUDGE_TEMPERATURE,
        "candidate_count": coverage["candidate_count"],
        "judged_count": coverage["judged_count"],
        "match_count": coverage["match_count"],
        "matches": coverage["matches"],
        "judgments": parsed["judgments"],
        "used_reference_count": None,
        "found": found,
        "must_not_treat_as_zero_hits": False,
    }


def answer_is_abstention(answer: str) -> bool:
    text = str(answer or "")
    return any(m in text for m in ABSTENTION_MARKERS)


def is_false_abstention(*, answer: str, target_hit_in_retrieval_or_context: bool) -> bool:
    """拒答 + 目标已在检索候选或真实 context 中命中 => false abstention。"""
    return bool(target_hit_in_retrieval_or_context) and answer_is_abstention(answer)


def stability_agreement(runs: Sequence[Sequence[str]]) -> float:
    """Exact-set agreement across runs. Diagnostic only for lookup_all (R6/R5.5E+)."""
    if not runs:
        return 0.0
    canon = [tuple(sorted(str(x) for x in run)) for run in runs]
    base = canon[0]
    agree = sum(1 for c in canon if c == base)
    return agree / len(canon)


def pairwise_jaccard(runs: Sequence[Sequence[str]]) -> float:
    """Mean pairwise Jaccard over match key sets. Diagnostic only."""
    sets = [set(str(x) for x in run) for run in runs]
    if len(sets) < 2:
        return 1.0 if sets else 0.0
    scores: List[float] = []
    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            a, b = sets[i], sets[j]
            if not a and not b:
                scores.append(1.0)
            else:
                scores.append(len(a & b) / float(len(a | b)))
    return sum(scores) / len(scores) if scores else 0.0


def stability_run_quality_ok(
    *,
    match_recall: float,
    match_precision: float,
    coverage_ok: bool,
    found: bool,
    ok: bool,
    precision_floor: float = 0.70,
) -> bool:
    """lookup_all quality stability: per-run gates (not exact-set agreement)."""
    return (
        bool(ok)
        and bool(coverage_ok)
        and found is True
        and float(match_recall) >= 1.0
        and float(match_precision) >= float(precision_floor)
    )


def stability_quality_pass(run_metrics: Sequence[Mapping[str, Any]], *, precision_floor: float = 0.70) -> bool:
    if not run_metrics:
        return False
    return all(
        stability_run_quality_ok(
            match_recall=float(m.get("match_recall_at_gold") or 0),
            match_precision=float(m.get("match_precision_at_gold") or 0),
            coverage_ok=bool(m.get("coverage_ok")),
            found=bool(m.get("found")),
            ok=bool(m.get("ok")),
            precision_floor=precision_floor,
        )
        for m in run_metrics
    )


def redact_for_report(obj: Any) -> Any:
    if isinstance(obj, Mapping):
        out: Dict[str, Any] = {}
        for k, v in obj.items():
            key = str(k)
            kl = key.lower()
            if key in SENSITIVE_REPORT_KEYS or any(
                s in kl for s in ("password", "secret", "api_key", "token", "authorization")
            ):
                out[key] = "<redacted>"
                continue
            out[key] = redact_for_report(v)
        return out
    if isinstance(obj, list):
        return [redact_for_report(x) for x in obj]
    if isinstance(obj, str) and len(obj) > 500:
        return f"<redacted_long_str len={len(obj)}>"
    return obj


def backend_root_from_here() -> Path:
    # app/eval/r5_schema.py → parents[2] == backend/
    return Path(__file__).resolve().parents[2]


def private_eval_root(backend_root: Optional[Path] = None) -> Path:
    root = (backend_root or backend_root_from_here()).resolve()
    return (root / "test_artifacts" / "evals" / "r5").resolve()


def _is_symlink_or_junction(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
    except OSError:
        return True
    try:
        st = path.lstat()
        # Windows FILE_ATTRIBUTE_REPARSE_POINT
        if getattr(st, "st_file_attributes", 0) & 0x400:
            return True
    except OSError:
        return False
    return False


def _split_r5_root(target: Path) -> Optional[Tuple[Path, Tuple[str, ...]]]:
    """Split absolute/relative path into (.../test_artifacts/evals/r5, rel_parts)."""
    parts = list(target.parts)
    lower = [p.lower() for p in parts]
    marker = ("test_artifacts", "evals", "r5")
    for i in range(0, max(0, len(lower) - len(marker) + 1)):
        if tuple(lower[i : i + 3]) == marker:
            root = Path(*parts[: i + 3])
            # Preserve absolute roots on POSIX ("/app/...").
            if target.is_absolute() and not root.is_absolute():
                root = Path(os.sep).joinpath(*parts[: i + 3])
            return root, tuple(parts[i + 3 :])
    return None


def resolve_private_eval_path(
    path: Union[str, Path],
    *,
    backend_root: Optional[Path] = None,
    must_exist: bool = False,
) -> Path:
    """
    Resolve path under */test_artifacts/evals/r5.
    Rejects .. escape, absolute paths outside any r5 private root, symlink/junction.
    Absolute paths keep their own r5 root (do not relocate to another backend tree).
    """
    default_root = private_eval_root(backend_root)
    default_root.mkdir(parents=True, exist_ok=True)
    default_root = default_root.resolve()
    raw = Path(path)
    target = raw.resolve(strict=False) if raw.is_absolute() else (default_root / raw).resolve(strict=False)
    split = _split_r5_root(target)
    if split is None:
        raise ValueError(f"R5 private path escapes {PRIVATE_EVAL_RELDIR}: {path}")
    root, rel_parts = split
    root = root.resolve() if root.exists() else root
    canonical = root.joinpath(*rel_parts) if rel_parts else root
    cursor = root
    for part in rel_parts:
        cursor = cursor / part
        if cursor.exists() and _is_symlink_or_junction(cursor):
            raise ValueError(f"symlink/junction not allowed: {cursor}")
    if must_exist and not canonical.exists():
        raise FileNotFoundError(str(canonical))
    return canonical


def assert_private_path(
    path: Union[str, Path], *, backend_root: Optional[Path] = None
) -> Path:
    return resolve_private_eval_path(path, backend_root=backend_root)


def database_env_privacy_stamp(database_url: str = "") -> Dict[str, Any]:
    """Schema + host class only — never emit credentials or full URL."""
    host_class = "unset"
    schema = None
    raw = (database_url or os.environ.get("DATABASE_URL") or "").strip()
    if raw:
        try:
            parsed = urlparse(raw.replace("mysql+pymysql://", "mysql://"))
            host = (parsed.hostname or "").lower()
            schema = (parsed.path or "/").lstrip("/") or None
            if host in {"127.0.0.1", "localhost", "::1"}:
                host_class = "localhost"
            elif host in {"db", "mysql", "mariadb"}:
                host_class = "compose_service"
            elif host:
                host_class = "remote_redacted"
            else:
                host_class = "unknown"
        except Exception:
            host_class = "unparseable"
    download_raw = (os.getenv("RAG_EMBEDDING_ALLOW_DOWNLOAD") or "0").strip().lower()
    return {
        "database_host_class": host_class,
        "database_schema": schema,
        "embedding_download_allowed": download_raw not in {"0", "false", "no", "off", ""},
        "credentials_included": False,
    }


def validate_example_fixture_row(row: Mapping[str, Any]) -> List[str]:
    errors: List[str] = []
    if not row.get("example"):
        errors.append("example fixture rows must set example=true")
    if row.get("quality_claim_allowed") is True:
        errors.append("example fixture must not set quality_claim_allowed=true")
    if str(row.get("manual_label_status") or "") not in {"example", "synthetic"}:
        errors.append("example fixture manual_label_status must be example|synthetic")
    return errors
