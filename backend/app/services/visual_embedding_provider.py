"""
Visual embedding providers for attachment visual RAG (Phase 5C/5H).

Optional heavy providers must not import PIL/torch/open_clip at module import
time. Fake provider uses deterministic hash vectors for tests only.
"""
from __future__ import annotations

import hashlib
import importlib.util
import math
from abc import ABC, abstractmethod
from typing import Any, Dict, List

from app.config import settings

FAKE_EMBEDDING_DIM = 32
FAKE_PROVIDER_NAME = "fake"
FAKE_MODEL_NAME = "fake-hash-v1"
DISABLED_PROVIDER_NAME = "disabled"
DISABLED_MODEL_NAME = "disabled"
OPENCLIP_PROVIDER_NAME = "openclip"


class VisualEmbeddingProvider(ABC):
    """Lightweight provider protocol for visual embeddings."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def model_name(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def embedding_dim(self) -> int:
        raise NotImplementedError

    @property
    def enabled(self) -> bool:
        return False

    @property
    def reason(self) -> str:
        return "visual_rag_disabled"

    @abstractmethod
    def embed_image(self, image_ref: str, metadata: Dict[str, Any] | None = None) -> List[float]:
        raise NotImplementedError

    @abstractmethod
    def embed_text(self, query: str) -> List[float]:
        raise NotImplementedError


def _deterministic_hash_vector(key: str, dim: int) -> List[float]:
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    values: List[float] = []
    idx = 0
    while len(values) < dim:
        chunk = digest[idx % len(digest): (idx % len(digest)) + 4]
        if len(chunk) < 4:
            chunk = hashlib.sha256((key + str(idx)).encode("utf-8")).digest()[:4]
        raw = int.from_bytes(chunk, "big", signed=False)
        values.append((raw / 2**32) * 2.0 - 1.0)
        idx += 4
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return [v / norm for v in values]


class DisabledVisualEmbeddingProvider(VisualEmbeddingProvider):
    def __init__(self, reason: str = "visual_rag_disabled") -> None:
        self._reason = reason

    @property
    def provider_name(self) -> str:
        return DISABLED_PROVIDER_NAME

    @property
    def model_name(self) -> str:
        return DISABLED_MODEL_NAME

    @property
    def embedding_dim(self) -> int:
        return 0

    @property
    def enabled(self) -> bool:
        return False

    @property
    def reason(self) -> str:
        return self._reason

    def embed_image(self, image_ref: str, metadata: Dict[str, Any] | None = None) -> List[float]:
        _ = (image_ref, metadata)
        return []

    def embed_text(self, query: str) -> List[float]:
        _ = query
        return []


class FakeVisualEmbeddingProvider(VisualEmbeddingProvider):
    @property
    def provider_name(self) -> str:
        return FAKE_PROVIDER_NAME

    @property
    def model_name(self) -> str:
        return FAKE_MODEL_NAME

    @property
    def embedding_dim(self) -> int:
        return FAKE_EMBEDDING_DIM

    @property
    def enabled(self) -> bool:
        return True

    @property
    def reason(self) -> str:
        return "visual_rag_fake_provider"

    def embed_image(self, image_ref: str, metadata: Dict[str, Any] | None = None) -> List[float]:
        meta = metadata or {}
        key = "|".join([
            "image",
            image_ref or "",
            str(meta.get("content_hash") or ""),
            str(meta.get("mime_type") or ""),
        ])
        return _deterministic_hash_vector(key, FAKE_EMBEDDING_DIM)

    def embed_text(self, query: str) -> List[float]:
        return _deterministic_hash_vector(f"query:{(query or '').strip()}", FAKE_EMBEDDING_DIM)


class OpenCLIPVisualEmbeddingProvider(VisualEmbeddingProvider):
    """OpenCLIP image/text embedding provider, loaded only when explicitly enabled."""

    def __init__(self) -> None:
        self._model: Any | None = None
        self._preprocess: Any | None = None
        self._tokenizer: Any | None = None
        self._torch: Any | None = None
        self._device: str | None = None
        self._embedding_dim: int | None = None

    @property
    def provider_name(self) -> str:
        return OPENCLIP_PROVIDER_NAME

    @property
    def model_name(self) -> str:
        return f"{settings.visual_rag_model}/{settings.visual_rag_pretrained}"

    @property
    def embedding_dim(self) -> int:
        return int(self._embedding_dim or settings.visual_rag_embedding_dim)

    @property
    def enabled(self) -> bool:
        return self._missing_dependency_reason() is None

    @property
    def reason(self) -> str:
        return self._missing_dependency_reason() or "visual_rag_openclip_provider"

    def _missing_dependency_reason(self) -> str | None:
        for module_name in ("open_clip", "torch", "PIL"):
            if importlib.util.find_spec(module_name) is None:
                return f"openclip_dependency_missing:{module_name}"
        return None

    def _resolve_device(self, torch_module: Any) -> str:
        configured = (settings.visual_rag_device or "cpu").strip().lower()
        if configured == "auto":
            return "cuda" if torch_module.cuda.is_available() else "cpu"
        if configured.startswith("cuda") and not torch_module.cuda.is_available():
            return "cpu"
        return configured or "cpu"

    def _ensure_loaded(self) -> None:
        if self._model is not None and self._preprocess is not None and self._tokenizer is not None:
            return
        missing_reason = self._missing_dependency_reason()
        if missing_reason is not None:
            raise RuntimeError(missing_reason)

        import open_clip  # type: ignore[import-not-found]
        import torch  # type: ignore[import-not-found]

        device = self._resolve_device(torch)
        model, _, preprocess = open_clip.create_model_and_transforms(
            settings.visual_rag_model,
            pretrained=settings.visual_rag_pretrained,
            device=device,
        )
        model.eval()
        self._model = model
        self._preprocess = preprocess
        self._tokenizer = open_clip.get_tokenizer(settings.visual_rag_model)
        self._torch = torch
        self._device = device

    def _normalized_vector(self, features: Any) -> List[float]:
        norm = features.norm(dim=-1, keepdim=True)
        features = features / norm.clamp(min=1e-12)
        vector = features[0].detach().cpu().float().tolist()
        self._embedding_dim = len(vector)
        return [float(value) for value in vector]

    def embed_image(self, image_ref: str, metadata: Dict[str, Any] | None = None) -> List[float]:
        _ = metadata
        if not image_ref:
            raise ValueError("image_ref is required for OpenCLIP image embedding")
        self._ensure_loaded()

        from PIL import Image  # type: ignore[import-not-found]

        assert self._torch is not None
        assert self._model is not None
        assert self._preprocess is not None
        assert self._device is not None

        with Image.open(image_ref) as image:
            image_tensor = self._preprocess(image.convert("RGB")).unsqueeze(0).to(self._device)
        with self._torch.no_grad():
            features = self._model.encode_image(image_tensor)
        return self._normalized_vector(features)

    def embed_text(self, query: str) -> List[float]:
        query_text = (query or "").strip()
        if not query_text:
            raise ValueError("query is required for OpenCLIP text embedding")
        self._ensure_loaded()

        assert self._torch is not None
        assert self._model is not None
        assert self._tokenizer is not None
        assert self._device is not None

        text_tensor = self._tokenizer([query_text]).to(self._device)
        with self._torch.no_grad():
            features = self._model.encode_text(text_tensor)
        return self._normalized_vector(features)


def get_visual_embedding_provider() -> VisualEmbeddingProvider:
    provider_key = (settings.visual_rag_provider or "disabled").strip().lower()
    if provider_key == "fake":
        return FakeVisualEmbeddingProvider()
    if provider_key in {OPENCLIP_PROVIDER_NAME, "open_clip"}:
        return OpenCLIPVisualEmbeddingProvider()
    if provider_key == "disabled":
        return DisabledVisualEmbeddingProvider(reason="visual_rag_disabled")
    return DisabledVisualEmbeddingProvider(reason=f"unknown_visual_rag_provider:{provider_key}")
