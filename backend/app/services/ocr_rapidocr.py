"""
R5.5A fix: RapidOCR PP-OCRv6 small fallback.

- Exact model filenames only (never rglob / never medium).
- Short-lived subprocess; OCR body only in a temp JSON file.
- Parent never logs OCR body or image paths.
"""
from __future__ import annotations

import json
import logging
import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

OCR_RAPIDOCR_UNAVAILABLE = "OCR_RAPIDOCR_UNAVAILABLE"
OCR_RAPIDOCR_MODELS_MISSING = "OCR_RAPIDOCR_MODELS_MISSING"
OCR_RAPIDOCR_TIMEOUT = "OCR_RAPIDOCR_TIMEOUT"
OCR_RAPIDOCR_OUTPUT_LIMIT = "OCR_RAPIDOCR_OUTPUT_LIMIT"
OCR_RAPIDOCR_FAILED = "OCR_RAPIDOCR_FAILED"

PROVIDER_NAME = "rapidocr_ppocrv6_small"
MODEL_LABEL = "PP-OCRv6_small_ONNX"

# Locked filenames — do not fuzzy-match or prefer medium.
DEFAULT_DET_NAME = "PP-OCRv6_det_small.onnx"
DEFAULT_REC_NAME = "PP-OCRv6_rec_small.onnx"
DEFAULT_CLS_NAME = "ch_ppocr_mobile_v2.0_cls_mobile.onnx"
REQUIRED_MODEL_NAMES = (DEFAULT_DET_NAME, DEFAULT_REC_NAME, DEFAULT_CLS_NAME)


def _settings():
    from app.config import settings

    return settings


def rapidocr_fallback_enabled() -> bool:
    return bool(getattr(_settings(), "attachment_rapidocr_fallback_enabled", True))


def rapidocr_timeout_sec() -> float:
    raw = getattr(_settings(), "attachment_rapidocr_timeout_sec", 60) or 60
    return max(5.0, min(float(raw), 300.0))


def rapidocr_max_output_bytes() -> int:
    raw = getattr(_settings(), "attachment_rapidocr_max_output_bytes", 256_000) or 256_000
    return max(1024, min(int(raw), 2_000_000))


def _model_dir() -> Path:
    configured = (getattr(_settings(), "attachment_rapidocr_model_dir", None) or "").strip()
    if configured:
        return Path(configured)
    backend_root = Path(__file__).resolve().parents[2]
    return backend_root / "models" / "rapidocr_ppocrv6_small"


def _exact_package_model_dir() -> Optional[Path]:
    """Return rapidocr/models dir only if all three exact small files exist."""
    try:
        import rapidocr  # type: ignore

        root = Path(rapidocr.__file__).resolve().parent / "models"
    except Exception:
        return None
    if not root.is_dir():
        return None
    for name in REQUIRED_MODEL_NAMES:
        if not (root / name).is_file():
            return None
        if "medium" in name.lower():
            return None
    return root


def resolve_rapidocr_model_paths() -> Tuple[Optional[Dict[str, Path]], Optional[str]]:
    """
    Resolve exact det/rec/cls ONNX paths. Never downloads. Never rglob.

    Returns: (paths_dict | None, error_code | None)
    """
    det_name = (
        getattr(_settings(), "attachment_rapidocr_det_name", None) or DEFAULT_DET_NAME
    ).strip()
    rec_name = (
        getattr(_settings(), "attachment_rapidocr_rec_name", None) or DEFAULT_REC_NAME
    ).strip()
    cls_name = (
        getattr(_settings(), "attachment_rapidocr_cls_name", None) or DEFAULT_CLS_NAME
    ).strip()

    # Enforce locked product names (reject medium / mismatched).
    if det_name != DEFAULT_DET_NAME or rec_name != DEFAULT_REC_NAME or cls_name != DEFAULT_CLS_NAME:
        return None, OCR_RAPIDOCR_MODELS_MISSING
    if any("medium" in n.lower() for n in (det_name, rec_name, cls_name)):
        return None, OCR_RAPIDOCR_MODELS_MISSING

    search_dirs = [_model_dir()]
    pkg = _exact_package_model_dir()
    if pkg is not None:
        search_dirs.append(pkg)

    for model_dir in search_dirs:
        candidates = {
            "det": model_dir / det_name,
            "rec": model_dir / rec_name,
            "cls": model_dir / cls_name,
        }
        if all(p.is_file() for p in candidates.values()):
            # Double-check basenames are exact small models.
            if any("medium" in p.name.lower() for p in candidates.values()):
                return None, OCR_RAPIDOCR_MODELS_MISSING
            return candidates, None

    return None, OCR_RAPIDOCR_MODELS_MISSING


def check_rapidocr_runtime() -> Dict[str, Any]:
    """Safe preflight — no paths, no model bytes, no download."""
    report: Dict[str, Any] = {
        "provider": PROVIDER_NAME,
        "model": MODEL_LABEL,
        "python_package": {"name": "rapidocr", "available": False},
        "onnxruntime": {"available": False},
        "models_ready": False,
        "ready": False,
        "error_code": OCR_RAPIDOCR_UNAVAILABLE,
        "required_model_names": list(REQUIRED_MODEL_NAMES),
    }
    try:
        import rapidocr  # noqa: F401  # type: ignore

        report["python_package"]["available"] = True
    except ImportError:
        return report
    try:
        import onnxruntime  # noqa: F401  # type: ignore

        report["onnxruntime"]["available"] = True
    except ImportError:
        return report

    paths, err = resolve_rapidocr_model_paths()
    if err or not paths:
        report["error_code"] = err or OCR_RAPIDOCR_MODELS_MISSING
        return report
    report["models_ready"] = True
    report["ready"] = True
    report["error_code"] = "OCR_READY"
    report["selected_model_basenames"] = {
        "det": paths["det"].name,
        "rec": paths["rec"].name,
        "cls": paths["cls"].name,
    }
    return report


def _worker_module() -> str:
    return "app.services.ocr_rapidocr_worker"


def _restrict_file_permissions(path: Path) -> None:
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def run_rapidocr_subprocess(image_path: Path) -> Dict[str, Any]:
    """
    Execute RapidOCR in a short-lived child process.

    OCR body travels only via a temp JSON file (not stdout/stderr).
    """
    started = time.perf_counter()
    empty = {
        "ok": False,
        "text": "",
        "mean_confidence": None,
        "error_code": OCR_RAPIDOCR_UNAVAILABLE,
        "provider": PROVIDER_NAME,
        "model": MODEL_LABEL,
        "model_files": {},
        "elapsed_ms": 0.0,
    }
    if not rapidocr_fallback_enabled():
        return empty

    paths, err = resolve_rapidocr_model_paths()
    if err or not paths:
        out = dict(empty)
        out["error_code"] = err or OCR_RAPIDOCR_MODELS_MISSING
        out["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
        return out

    model_files = {
        "det": paths["det"].name,
        "rec": paths["rec"].name,
        "cls": paths["cls"].name,
    }
    if any("medium" in n.lower() for n in model_files.values()):
        out = dict(empty)
        out["error_code"] = OCR_RAPIDOCR_MODELS_MISSING
        out["model_files"] = model_files
        out["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
        return out

    env = os.environ.copy()
    env["OMP_NUM_THREADS"] = "1"
    env["MKL_NUM_THREADS"] = "1"
    env["OPENBLAS_NUM_THREADS"] = "1"
    env["NUMEXPR_NUM_THREADS"] = "1"
    env["RAG_RAPIDOCR_THREADS"] = "1"
    env["RAPIDOCR_OFFLINE"] = "1"
    env["HF_HUB_OFFLINE"] = "1"

    max_bytes = rapidocr_max_output_bytes()
    timeout = rapidocr_timeout_sec()
    out_path: Optional[Path] = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix="rapidocr_", suffix=".json")
        os.close(fd)
        out_path = Path(tmp_name)
        _restrict_file_permissions(out_path)
        # Truncate so child overwrites cleanly.
        out_path.write_text("", encoding="utf-8")

        req = {
            "image_path": str(image_path),
            "output_path": str(out_path),
            "det_path": str(paths["det"]),
            "rec_path": str(paths["rec"]),
            "cls_path": str(paths["cls"]),
        }
        cmd = [sys.executable, "-m", _worker_module()]
        # Windows: passing Unicode JSON via text=True stdin can corrupt non-ASCII
        # paths (e.g. CJK directories). Always send UTF-8 bytes.
        req_bytes = json.dumps(req, ensure_ascii=False).encode("utf-8")
        try:
            proc = subprocess.run(
                cmd,
                input=req_bytes,
                capture_output=True,
                timeout=timeout,
                env=env,
                check=False,
            )
        except subprocess.TimeoutExpired:
            logger.warning("rapidocr subprocess timeout code=%s", OCR_RAPIDOCR_TIMEOUT)
            return {
                "ok": False,
                "text": "",
                "mean_confidence": None,
                "error_code": OCR_RAPIDOCR_TIMEOUT,
                "provider": PROVIDER_NAME,
                "model": MODEL_LABEL,
                "model_files": model_files,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            }
        except Exception:
            logger.warning("rapidocr subprocess failed code=%s", OCR_RAPIDOCR_FAILED)
            return {
                "ok": False,
                "text": "",
                "mean_confidence": None,
                "error_code": OCR_RAPIDOCR_FAILED,
                "provider": PROVIDER_NAME,
                "model": MODEL_LABEL,
                "model_files": model_files,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            }

        # stdout/stderr must not carry OCR body; only inspect length / safe JSON status.
        stdout = (proc.stdout or b"").decode("utf-8", errors="replace")
        stderr = (proc.stderr or b"").decode("utf-8", errors="replace")
        if len(stdout.encode("utf-8", errors="ignore")) > 512:
            logger.warning("rapidocr stdout over safe status limit")
            return {
                "ok": False,
                "text": "",
                "mean_confidence": None,
                "error_code": OCR_RAPIDOCR_FAILED,
                "provider": PROVIDER_NAME,
                "model": MODEL_LABEL,
                "model_files": model_files,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            }
        if stderr.strip():
            # Do not log stderr body (may leak).
            logger.warning("rapidocr stderr non-empty code=%s", OCR_RAPIDOCR_FAILED)

        if not out_path.is_file():
            return {
                "ok": False,
                "text": "",
                "mean_confidence": None,
                "error_code": OCR_RAPIDOCR_FAILED,
                "provider": PROVIDER_NAME,
                "model": MODEL_LABEL,
                "model_files": model_files,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            }

        size = out_path.stat().st_size
        if size > max_bytes:
            logger.warning("rapidocr output over limit code=%s", OCR_RAPIDOCR_OUTPUT_LIMIT)
            return {
                "ok": False,
                "text": "",
                "mean_confidence": None,
                "error_code": OCR_RAPIDOCR_OUTPUT_LIMIT,
                "provider": PROVIDER_NAME,
                "model": MODEL_LABEL,
                "model_files": model_files,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            }
        if size <= 0:
            return {
                "ok": False,
                "text": "",
                "mean_confidence": None,
                "error_code": OCR_RAPIDOCR_FAILED,
                "provider": PROVIDER_NAME,
                "model": MODEL_LABEL,
                "model_files": model_files,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            }

        try:
            payload = json.loads(out_path.read_text(encoding="utf-8"))
        except Exception:
            return {
                "ok": False,
                "text": "",
                "mean_confidence": None,
                "error_code": OCR_RAPIDOCR_FAILED,
                "provider": PROVIDER_NAME,
                "model": MODEL_LABEL,
                "model_files": model_files,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            }

        if not isinstance(payload, dict):
            return {
                "ok": False,
                "text": "",
                "mean_confidence": None,
                "error_code": OCR_RAPIDOCR_FAILED,
                "provider": PROVIDER_NAME,
                "model": MODEL_LABEL,
                "model_files": model_files,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            }

        text = str(payload.get("text") or "")
        conf = payload.get("mean_confidence")
        mean_conf = float(conf) if isinstance(conf, (int, float)) else None
        ok = bool(payload.get("ok")) and bool(text.strip())
        error_code = payload.get("error_code")
        files = payload.get("model_files") if isinstance(payload.get("model_files"), dict) else model_files
        return {
            "ok": ok,
            "text": text if ok else "",
            "mean_confidence": mean_conf,
            "error_code": None if ok else (str(error_code) if error_code else OCR_RAPIDOCR_FAILED),
            "provider": PROVIDER_NAME,
            "model": MODEL_LABEL,
            "model_files": files,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        }
    finally:
        if out_path is not None:
            try:
                out_path.unlink(missing_ok=True)
            except OSError:
                pass
