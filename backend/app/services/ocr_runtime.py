"""
OCR runtime preflight and controlled errors.

Default provider: pytesseract / Tesseract 5.
PaddleOCR is an extension hook only — not installed or claimed verified here.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# Stable error codes (safe for metadata / JSON; no paths or exception text).
OCR_DISABLED = "OCR_DISABLED"
OCR_PROVIDER_UNSUPPORTED = "OCR_PROVIDER_UNSUPPORTED"
OCR_PYTHON_PACKAGE_MISSING = "OCR_PYTHON_PACKAGE_MISSING"
OCR_ENGINE_UNAVAILABLE = "OCR_ENGINE_UNAVAILABLE"
OCR_LANG_MISSING = "OCR_LANG_MISSING"
OCR_PADDLE_EXTENSION_UNAVAILABLE = "OCR_PADDLE_EXTENSION_UNAVAILABLE"
OCR_RAPIDOCR_UNAVAILABLE = "OCR_RAPIDOCR_UNAVAILABLE"
OCR_RAPIDOCR_MODELS_MISSING = "OCR_RAPIDOCR_MODELS_MISSING"
OCR_FAILED = "OCR_FAILED"
OCR_READY = "OCR_READY"

OCR_STATUS_SUCCEEDED = "succeeded"
OCR_STATUS_EMPTY = "empty"
OCR_STATUS_DISABLED = "disabled"
OCR_STATUS_UNAVAILABLE = "unavailable"
OCR_STATUS_FAILED = "failed"

VALID_OCR_STATUSES = frozenset(
    {
        OCR_STATUS_SUCCEEDED,
        OCR_STATUS_EMPTY,
        OCR_STATUS_DISABLED,
        OCR_STATUS_UNAVAILABLE,
        OCR_STATUS_FAILED,
    }
)


class OcrRequiredError(RuntimeError):
    """Raised when image OCR is required but unavailable or failed."""

    def __init__(self, message: str = "图片文字识别暂不可用", *, error_code: str = OCR_ENGINE_UNAVAILABLE):
        super().__init__(message)
        self.error_code = error_code


class PdfScanOcrUnavailableError(RuntimeError):
    """Raised when PDF OCR fallback is on but a textless page cannot be OCR'd."""

    def __init__(
        self,
        message: str = "PDF 扫描页文字识别暂不可用，请稍后重试",
        *,
        error_code: str = OCR_ENGINE_UNAVAILABLE,
    ):
        super().__init__(message)
        self.error_code = error_code


def _settings():
    from app.config import settings

    return settings


def normalize_provider(raw: Optional[str] = None) -> str:
    value = (raw if raw is not None else _settings().attachment_ocr_provider) or "pytesseract"
    return str(value).strip().lower()


def tesseract_lang_codes(lang: Optional[str] = None) -> List[str]:
    raw = lang if lang is not None else _settings().attachment_tesseract_lang
    parts = [p.strip() for p in str(raw or "chi_sim+eng").split("+") if p.strip()]
    return parts or ["chi_sim", "eng"]


def normalize_ocr_status(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    value = str(raw).strip().lower()
    if value == "available":
        return OCR_STATUS_SUCCEEDED
    if value in VALID_OCR_STATUSES:
        return value
    return None


def check_ocr_runtime(*, provider: Optional[str] = None) -> Dict[str, Any]:
    """Return a safe JSON-serializable OCR readiness report (no paths/secrets)."""
    prov = normalize_provider(provider)
    required = bool(_settings().attachment_image_ocr_required)
    base: Dict[str, Any] = {
        "provider": prov,
        "python_package": {"name": None, "available": False},
        "engine": {"name": None, "available": False},
        "languages": {
            "required": [],
            "available": [],
            "missing": [],
        },
        "ready": False,
        "error_code": OCR_ENGINE_UNAVAILABLE,
        "ocr_required": required,
    }

    if prov in {"disabled", "none", "off"}:
        base["python_package"] = {"name": None, "available": True}
        base["engine"] = {"name": "disabled", "available": False}
        base["error_code"] = OCR_DISABLED
        base["ready"] = False
        return base

    if prov in {"pytesseract", "tesseract"}:
        return _check_pytesseract(base)

    if prov == "paddleocr":
        return _check_paddleocr_extension(base)

    if prov in {"rapidocr", "rapidocr_ppocrv6_small"}:
        from app.services.ocr_rapidocr import check_rapidocr_runtime

        rapid = check_rapidocr_runtime()
        base["python_package"] = rapid.get("python_package") or base["python_package"]
        base["engine"] = {"name": "rapidocr", "available": bool(rapid.get("ready"))}
        base["ready"] = bool(rapid.get("ready"))
        base["error_code"] = rapid.get("error_code") or OCR_RAPIDOCR_UNAVAILABLE
        base["rapidocr"] = {
            "model": rapid.get("model"),
            "models_ready": rapid.get("models_ready"),
            "onnxruntime": rapid.get("onnxruntime"),
        }
        return base

    base["error_code"] = OCR_PROVIDER_UNSUPPORTED
    return base


def _check_pytesseract(base: Dict[str, Any]) -> Dict[str, Any]:
    base["python_package"] = {"name": "pytesseract", "available": False}
    base["engine"] = {"name": "tesseract", "available": False}
    required_langs = tesseract_lang_codes()
    base["languages"]["required"] = list(required_langs)

    try:
        import pytesseract  # type: ignore
    except ImportError:
        base["error_code"] = OCR_PYTHON_PACKAGE_MISSING
        return base

    base["python_package"]["available"] = True

    cmd = (_settings().attachment_tesseract_cmd or "").strip()
    previous_cmd = None
    try:
        if cmd:
            previous_cmd = getattr(pytesseract.pytesseract, "tesseract_cmd", None)
            pytesseract.pytesseract.tesseract_cmd = cmd
        try:
            pytesseract.get_tesseract_version()
            base["engine"]["available"] = True
        except Exception:
            base["error_code"] = OCR_ENGINE_UNAVAILABLE
            return base

        try:
            langs = list(pytesseract.get_languages(config="") or [])
        except Exception:
            langs = []
        available = [code for code in required_langs if code in langs]
        missing = [code for code in required_langs if code not in langs]
        base["languages"]["available"] = available
        base["languages"]["missing"] = missing
        if missing:
            base["error_code"] = OCR_LANG_MISSING
            base["ready"] = False
            return base

        base["ready"] = True
        base["error_code"] = OCR_READY
        return base
    finally:
        if cmd and previous_cmd is not None:
            try:
                pytesseract.pytesseract.tesseract_cmd = previous_cmd
            except Exception:
                pass


def _check_paddleocr_extension(base: Dict[str, Any]) -> Dict[str, Any]:
    """PaddleOCR is an optional extension; never download models in preflight."""
    base["python_package"] = {"name": "paddleocr", "available": False}
    base["engine"] = {"name": "paddleocr", "available": False}
    base["languages"]["required"] = [str(_settings().attachment_paddleocr_lang or "ch")]
    try:
        import paddleocr  # noqa: F401  # type: ignore
    except ImportError:
        base["error_code"] = OCR_PADDLE_EXTENSION_UNAVAILABLE
        return base
    # Package present still does not mean models/runtime verified in this phase.
    base["python_package"]["available"] = True
    base["error_code"] = OCR_PADDLE_EXTENSION_UNAVAILABLE
    base["ready"] = False
    return base
