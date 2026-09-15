"""
Short-lived RapidOCR worker process (R5.5A fix).

Protocol:
  - Parent sends one JSON line on stdin: image_path, output_path, model paths.
  - Child writes result JSON (may contain OCR text) only to output_path.
  - stdout may only contain a tiny safe status object (no OCR body / paths).
  - stderr must stay empty of OCR body / paths / stacks.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, List, Optional


def _set_thread_env() -> None:
    for key in (
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
    ):
        os.environ[key] = "1"


def _mean_confidence(scores: List[float]) -> Optional[float]:
    if not scores:
        return None
    return float(sum(scores) / len(scores))


def _safe_list(value: Any) -> List[Any]:
    """NumPy-safe: never use `ndarray or []`."""
    if value is None:
        return []
    return list(value)


def _write_output(path: str, payload: dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)


def _stdout_status(*, ok: bool, error_code: Optional[str]) -> None:
    # No OCR body, no paths.
    sys.stdout.write(
        json.dumps({"ok": bool(ok), "error_code": error_code}, ensure_ascii=False)
    )


def _run(req: dict[str, Any]) -> dict[str, Any]:
    image_path = str(req.get("image_path") or "").strip()
    output_path = str(req.get("output_path") or "").strip()
    det = str(req.get("det_path") or "").strip()
    rec = str(req.get("rec_path") or "").strip()
    cls = str(req.get("cls_path") or "").strip()

    if not output_path:
        return {"ok": False, "text": "", "mean_confidence": None, "error_code": "OCR_RAPIDOCR_FAILED",
                "provider": "rapidocr_ppocrv6_small", "model": "PP-OCRv6_small_ONNX",
                "model_files": {}}

    model_files = {
        "det": os.path.basename(det) if det else "",
        "rec": os.path.basename(rec) if rec else "",
        "cls": os.path.basename(cls) if cls else "",
    }
    # Reject medium selection even if misconfigured.
    for name in (model_files["det"], model_files["rec"]):
        if "medium" in name.lower():
            payload = {
                "ok": False,
                "text": "",
                "mean_confidence": None,
                "error_code": "OCR_RAPIDOCR_MODELS_MISSING",
                "provider": "rapidocr_ppocrv6_small",
                "model": "PP-OCRv6_small_ONNX",
                "model_files": model_files,
            }
            _write_output(output_path, payload)
            return payload

    required_names = {
        "det": "PP-OCRv6_det_small.onnx",
        "rec": "PP-OCRv6_rec_small.onnx",
        "cls": "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
    }
    if (
        model_files["det"] != required_names["det"]
        or model_files["rec"] != required_names["rec"]
        or model_files["cls"] != required_names["cls"]
    ):
        payload = {
            "ok": False,
            "text": "",
            "mean_confidence": None,
            "error_code": "OCR_RAPIDOCR_MODELS_MISSING",
            "provider": "rapidocr_ppocrv6_small",
            "model": "PP-OCRv6_small_ONNX",
            "model_files": model_files,
        }
        _write_output(output_path, payload)
        return payload

    if not (image_path and det and rec and cls):
        payload = {
            "ok": False,
            "text": "",
            "mean_confidence": None,
            "error_code": "OCR_RAPIDOCR_MODELS_MISSING",
            "provider": "rapidocr_ppocrv6_small",
            "model": "PP-OCRv6_small_ONNX",
            "model_files": model_files,
        }
        _write_output(output_path, payload)
        return payload
    if not (os.path.isfile(det) and os.path.isfile(rec) and os.path.isfile(cls) and os.path.isfile(image_path)):
        payload = {
            "ok": False,
            "text": "",
            "mean_confidence": None,
            "error_code": "OCR_RAPIDOCR_MODELS_MISSING",
            "provider": "rapidocr_ppocrv6_small",
            "model": "PP-OCRv6_small_ONNX",
            "model_files": model_files,
        }
        _write_output(output_path, payload)
        return payload

    _set_thread_env()
    try:
        from rapidocr import RapidOCR  # type: ignore
    except Exception:
        payload = {
            "ok": False,
            "text": "",
            "mean_confidence": None,
            "error_code": "OCR_RAPIDOCR_UNAVAILABLE",
            "provider": "rapidocr_ppocrv6_small",
            "model": "PP-OCRv6_small_ONNX",
            "model_files": model_files,
        }
        _write_output(output_path, payload)
        return payload

    params = {
        "EngineConfig.onnxruntime.intra_op_num_threads": 1,
        "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        "EngineConfig.onnxruntime.enable_cpu_mem_arena": False,
        "Global.log_level": "error",
        "Det.model_path": det,
        "Rec.model_path": rec,
        "Cls.model_path": cls,
    }
    try:
        engine = RapidOCR(params=params)
        result = engine(image_path)
    except Exception:
        payload = {
            "ok": False,
            "text": "",
            "mean_confidence": None,
            "error_code": "OCR_RAPIDOCR_FAILED",
            "provider": "rapidocr_ppocrv6_small",
            "model": "PP-OCRv6_small_ONNX",
            "model_files": model_files,
        }
        _write_output(output_path, payload)
        return payload

    texts: List[str] = []
    scores: List[float] = []
    payload_obj = result
    if isinstance(result, tuple) and result:
        payload_obj = result[0] if len(result) == 1 else result

    if hasattr(payload_obj, "txts"):
        for t in _safe_list(getattr(payload_obj, "txts", None)):
            if t is None:
                continue
            s = str(t).strip()
            if s:
                texts.append(s)
        for s in _safe_list(getattr(payload_obj, "scores", None)):
            try:
                scores.append(float(s))
            except Exception:
                pass
        # boxes parsed for completeness / future use; never logged
        _ = _safe_list(getattr(payload_obj, "boxes", None))
    elif isinstance(payload_obj, (list, tuple)):
        for item in payload_obj:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                mid = item[1]
                if isinstance(mid, (list, tuple)) and mid:
                    texts.append(str(mid[0]))
                    if len(mid) > 1:
                        try:
                            scores.append(float(mid[1]))
                        except Exception:
                            pass
                elif isinstance(mid, str):
                    texts.append(mid)

    text = "\n".join(texts).strip()
    out = {
        "ok": bool(text),
        "text": text,
        "mean_confidence": _mean_confidence(scores),
        "error_code": None if text else None,
        "provider": "rapidocr_ppocrv6_small",
        "model": "PP-OCRv6_small_ONNX",
        "model_files": model_files,
    }
    _write_output(output_path, out)
    return out


def main() -> int:
    try:
        raw = sys.stdin.read()
        req = json.loads(raw) if raw.strip() else {}
    except Exception:
        _stdout_status(ok=False, error_code="OCR_RAPIDOCR_FAILED")
        return 2
    if not isinstance(req, dict):
        _stdout_status(ok=False, error_code="OCR_RAPIDOCR_FAILED")
        return 2
    try:
        result = _run(req)
    except Exception:
        # Never dump stacks / paths / OCR body.
        out_path = str((req or {}).get("output_path") or "").strip()
        if out_path:
            try:
                _write_output(
                    out_path,
                    {
                        "ok": False,
                        "text": "",
                        "mean_confidence": None,
                        "error_code": "OCR_RAPIDOCR_FAILED",
                        "provider": "rapidocr_ppocrv6_small",
                        "model": "PP-OCRv6_small_ONNX",
                        "model_files": {},
                    },
                )
            except Exception:
                pass
        _stdout_status(ok=False, error_code="OCR_RAPIDOCR_FAILED")
        return 1
    _stdout_status(ok=bool(result.get("ok")), error_code=result.get("error_code"))
    return 0 if result.get("ok") or result.get("error_code") is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
