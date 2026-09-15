"""
R5 private batch evidence verifier.

CHECKSUMS must cover every final file in the batch except itself.
No .tmp leftovers; required files present; SHAs match.
R5.4.3: private_manifest / public_summary / baseline_report batch_id+phase must agree.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Dict, List, Set

REQUIRED_FILES = frozenset(
    {
        "private_manifest.json",
        "public_summary.json",
        "cases.jsonl",
        "baseline_report.json",
        "drift_check.json",
        "prompt_gold_review_decisions.json",
        "CHECKSUMS.sha256",
    }
)

E_MISSING_FILE = "E_BATCH_MISSING_FILE"
E_TMP_RESIDUE = "E_BATCH_TMP_RESIDUE"
E_EXTRA_FILE = "E_BATCH_EXTRA_FILE"
E_CHECKSUM_PARSE = "E_BATCH_CHECKSUM_PARSE"
E_CHECKSUM_MISMATCH = "E_BATCH_CHECKSUM_MISMATCH"
E_CHECKSUM_SELF = "E_BATCH_CHECKSUM_SELF"
E_CHECKSUM_INCOMPLETE = "E_BATCH_CHECKSUM_INCOMPLETE"
E_BATCH_ID_MISMATCH = "E_BATCH_ID_MISMATCH"
E_BATCH_PHASE_MISMATCH = "E_BATCH_PHASE_MISMATCH"


class BatchVerifierError(ValueError):
    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


_LINE_RE = re.compile(r"^([0-9a-fA-F]{64})  (.+)$")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_checksums(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for i, line in enumerate(text.splitlines()):
        if not line.strip():
            continue
        m = _LINE_RE.match(line)
        if not m:
            raise BatchVerifierError(f"bad checksum line {i+1}", code=E_CHECKSUM_PARSE)
        digest, name = m.group(1).lower(), m.group(2)
        if name == "CHECKSUMS.sha256":
            raise BatchVerifierError("CHECKSUMS must not include itself", code=E_CHECKSUM_SELF)
        if name in out:
            raise BatchVerifierError(f"duplicate checksum entry {name}", code=E_CHECKSUM_PARSE)
        out[name] = digest
    return out


def write_checksums_atomic(batch_dir: Path) -> Path:
    """Write CHECKSUMS last over all final files except itself (atomic replace)."""
    batch_dir = Path(batch_dir)
    files = sorted(
        p.name
        for p in batch_dir.iterdir()
        if p.is_file() and p.name != "CHECKSUMS.sha256" and not p.name.endswith(".tmp")
    )
    lines = []
    for name in files:
        digest = _sha256_file(batch_dir / name)
        lines.append(f"{digest}  {name}\n")
    tmp = batch_dir / "CHECKSUMS.sha256.tmp"
    final = batch_dir / "CHECKSUMS.sha256"
    tmp.write_text("".join(lines), encoding="utf-8", newline="\n")
    tmp.replace(final)
    return final


def verify_r5_batch(batch_dir: Path, *, allow_extra: bool = False) -> Dict[str, object]:
    batch_dir = Path(batch_dir)
    if not batch_dir.is_dir():
        raise BatchVerifierError(f"missing batch dir: {batch_dir}", code=E_MISSING_FILE)

    names = {p.name for p in batch_dir.iterdir() if p.is_file()}
    tmp_files = sorted(n for n in names if n.endswith(".tmp") or n.startswith("."))
    # allow only known hidden? forbid .tmp always
    bad_tmp = sorted(n for n in names if n.endswith(".tmp"))
    if bad_tmp:
        raise BatchVerifierError(f"tmp residue: {bad_tmp}", code=E_TMP_RESIDUE)

    missing = sorted(REQUIRED_FILES - names)
    if missing:
        raise BatchVerifierError(f"missing required: {missing}", code=E_MISSING_FILE)

    if not allow_extra:
        # Allow only required + optional registered extras if needed later
        extras = sorted(names - REQUIRED_FILES)
        if extras:
            raise BatchVerifierError(f"extra files: {extras}", code=E_EXTRA_FILE)

    checksum_path = batch_dir / "CHECKSUMS.sha256"
    listed = parse_checksums(checksum_path.read_text(encoding="utf-8"))
    expected_names = names - {"CHECKSUMS.sha256"}
    if set(listed.keys()) != expected_names:
        raise BatchVerifierError(
            f"checksum set mismatch listed={sorted(listed)} disk={sorted(expected_names)}",
            code=E_CHECKSUM_INCOMPLETE,
        )
    for name, digest in listed.items():
        actual = _sha256_file(batch_dir / name)
        if actual.lower() != digest.lower():
            raise BatchVerifierError(f"sha mismatch for {name}", code=E_CHECKSUM_MISMATCH)

    # Cross-file batch_id / phase consistency (parent ids only via source/parent fields).
    manifest = json.loads((batch_dir / "private_manifest.json").read_text(encoding="utf-8"))
    public = json.loads((batch_dir / "public_summary.json").read_text(encoding="utf-8"))
    baseline = json.loads((batch_dir / "baseline_report.json").read_text(encoding="utf-8"))
    batch_id = str(manifest.get("batch_id") or "")
    phase = str(manifest.get("phase") or "")
    if not batch_id or not phase:
        raise BatchVerifierError(
            "private_manifest missing batch_id/phase",
            code=E_BATCH_ID_MISMATCH,
        )
    for label, doc in (("public_summary", public), ("baseline_report", baseline)):
        if str(doc.get("batch_id") or "") != batch_id:
            raise BatchVerifierError(
                f"{label} batch_id mismatch: {doc.get('batch_id')!r} != {batch_id!r}",
                code=E_BATCH_ID_MISMATCH,
            )
        if str(doc.get("phase") or "") != phase:
            raise BatchVerifierError(
                f"{label} phase mismatch: {doc.get('phase')!r} != {phase!r}",
                code=E_BATCH_PHASE_MISMATCH,
            )
    # Parent references must not overwrite identity fields.
    for forbidden in ("batch_id", "phase"):
        if forbidden in (manifest.get("parent_batches") or []):
            raise BatchVerifierError(
                "parent_batches must not embed identity fields",
                code=E_BATCH_ID_MISMATCH,
            )

    return {
        "ok": True,
        "file_count": len(names),
        "checksum_count": len(listed),
        "required_ok": True,
        "batch_id": batch_id,
        "phase": phase,
    }
