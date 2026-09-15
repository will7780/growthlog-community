"""
向量化服务
使用 intfloat/multilingual-e5-base 模型生成文本向量
"""
import os
from pathlib import Path
import numpy as np
from typing import TYPE_CHECKING, Any, List, Optional
if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

# 模型名称
MODEL_NAME = "intfloat/multilingual-e5-base"
# 向量维度
EMBEDDING_DIM = 768

# 本地模型路径（优先使用本地模型，避免网络下载）
# 模型目录：backend/models/multilingual-e5-base/
_LOCAL_MODEL_PATH = Path(__file__).parent.parent.parent / "models" / "multilingual-e5-base"

# 全局模型实例（延迟加载）
_model: Any = None


class LocalModelMissingError(RuntimeError):
    """Raised when local E5 weights are required but missing."""

    code = "E_LOCAL_MODEL_MISSING"


def local_model_path() -> Path:
    return _LOCAL_MODEL_PATH


def local_model_available() -> bool:
    path = _LOCAL_MODEL_PATH
    return path.exists() and (path / "config.json").exists()


def _allow_download() -> bool:
    # Default preserves historical online fallback. Live eval sets "0".
    raw = (os.getenv("RAG_EMBEDDING_ALLOW_DOWNLOAD") or "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def get_model() -> "SentenceTransformer":
    """获取或初始化模型（单例模式）"""
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        # 优先使用本地模型
        model_path = _LOCAL_MODEL_PATH
        if local_model_available():
            print(f"[Embedding] 从本地加载模型: {model_path}")
            _model = SentenceTransformer(str(model_path))
        else:
            if not _allow_download():
                raise LocalModelMissingError(
                    f"local embedding model missing at {model_path}; download disabled"
                )
            # 回退到在线下载（生产默认路径；D.3 live 评估禁止）
            print(f"[Embedding] 本地模型不存在，从 HuggingFace 下载: {MODEL_NAME}")
            print(f"[Embedding] 如下载缓慢，请手动下载模型到 {model_path}")
            _model = SentenceTransformer(MODEL_NAME)
        print("[Embedding] 模型加载完成")
    return _model


def generate_embedding(text: str, normalize: bool = True) -> List[float]:
    """
    生成文本的向量表示

    Args:
        text: 输入文本
        normalize: 是否归一化（E5 推荐归一化）

    Returns:
        768 维向量列表
    """
    if not text or not text.strip():
        return [0.0] * EMBEDDING_DIM

    model = get_model()
    embedding = model.encode(text, normalize_embeddings=normalize)
    return embedding.tolist()


def generate_title_content_embedding(title: str, content: str) -> dict:
    """
    为记录生成标题和内容的向量

    Args:
        title: 标题
        content: 内容

    Returns:
        {
            "title_vector": List[float],
            "content_vector": List[float]
        }
    """
    # E5 模型对标题和内容使用不同的前缀
    # 根据官方文档: "query" 前缀用于搜索，"passage" 前缀用于索引
    title_prefixed = f"passage: {title}" if title else "passage: "
    content_prefixed = f"passage: {content}" if content else "passage: "

    title_vector = generate_embedding(title_prefixed)
    content_vector = generate_embedding(content_prefixed)

    return {
        "title_vector": title_vector,
        "content_vector": content_vector
    }


def cosine_similarity(vec1: List[float], vec2: List[float]) -> float:
    """
    计算两个向量的余弦相似度

    Args:
        vec1: 向量1
        vec2: 向量2

    Returns:
        余弦相似度 (-1 到 1 之间)
    """
    v1 = np.array(vec1)
    v2 = np.array(vec2)

    norm1 = np.linalg.norm(v1)
    norm2 = np.linalg.norm(v2)

    if norm1 == 0 or norm2 == 0:
        return 0.0

    return float(np.dot(v1, v2) / (norm1 * norm2))
