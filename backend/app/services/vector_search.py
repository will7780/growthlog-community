"""
向量检索服务

提供两种检索模式：
1. FAISS 模式（默认）：使用内存 FAISS 索引进行高速检索
2. NumPy 模式（fallback）：使用 NumPy 批量计算作为备选

架构：
- VectorIndexManager: 管理用户的 FAISS 索引，支持 LRU 缓存和磁盘持久化
- 向量在记录创建/更新时添加到索引
- 检索时直接使用 FAISS 索引，无需查询数据库
"""
import json
import logging
import os
import numpy as np
from typing import List, Dict, Optional
from sqlalchemy.orm import Session
from sqlalchemy import bindparam, text

from app.services.vector_index import (
    VectorIndexManager,
    get_index_manager,
    UserVectorIndex,
)
from app.services.vector_storage_local import LocalStorageBackend

logger = logging.getLogger(__name__)

RELEVANCE_THRESHOLD = 0.35

# 全局索引管理器（延迟初始化）；与 VectorIndexManager 单例保持同步
_index_manager: Optional[VectorIndexManager] = None


def _resolved_vector_index_dir() -> str:
    """Honor GROWTHLOG_VECTOR_INDEX_DIR; default remains vector_indexes."""
    raw = (os.environ.get("GROWTHLOG_VECTOR_INDEX_DIR") or "").strip()
    return raw or "vector_indexes"


def _get_index_manager() -> VectorIndexManager:
    """获取索引管理器（单例）。环境变量必须在首次初始化前生效。"""
    global _index_manager
    singleton = VectorIndexManager._instance
    if singleton is None:
        resolved = _resolved_vector_index_dir()
        # LocalStorageBackend owns on-disk I/O; it must use the same resolved path.
        storage = LocalStorageBackend(base_path=resolved)
        singleton = VectorIndexManager.get_instance(
            storage_path=resolved,
            cache_size=100,
            index_dim=768,
            storage_backend=storage,
        )
    _index_manager = singleton
    return singleton


def search_similar_entry_hits(
    db: Session,
    query_vector: List[float],
    user_id: int,
    top_k: int = 5,
    min_score: float = RELEVANCE_THRESHOLD,
) -> List[Dict]:
    """
    FAISS/Numpy ID+score only — no per-row DB hydrate.

    Returns [{"entry_id": int, "relevance_score": float}, ...] in rank order.
    """
    try:
        manager = _get_index_manager()
        results = manager.search(user_id, query_vector, k=top_k, min_score=min_score) or []
        return [
            {
                "entry_id": int(r["entry_id"]),
                "relevance_score": float(r["relevance_score"]),
            }
            for r in results
            if r.get("entry_id") is not None
        ]
    except Exception as e:
        logger.error(f"FAISS search failed for user {user_id}: {e}, falling back to NumPy hits")
        numpy_rows = _search_similar_entries_numpy(
            db, query_vector, user_id, top_k, None, min_score
        )
        return [
            {
                "entry_id": int(r["entry_id"]),
                "relevance_score": float(r["relevance_score"]),
            }
            for r in numpy_rows
        ]


def bulk_get_entries_with_labels(
    db: Session,
    entry_ids: List[int],
    *,
    user_id: int,
    label_code: Optional[str] = None,
) -> Dict[int, Dict]:
    """
    One batched query for entries+labels. Preserves caller ordering separately.
    Query count does not grow with len(entry_ids) beyond a single IN clause.
    """
    ids = [int(i) for i in entry_ids if i is not None]
    if not ids:
        return {}
    # Deduplicate while keeping a stable bind list for SQL IN.
    uniq: List[int] = []
    seen = set()
    for i in ids:
        if i in seen:
            continue
        seen.add(i)
        uniq.append(i)

    sql = """
        SELECT
            e.id as entry_id,
            e.user_id,
            e.label_code,
            e.content,
            e.created_at,
            l.name as label_name
        FROM entries e
        LEFT JOIN entry_labels l ON e.label_code = l.code
        WHERE e.user_id = :user_id
          AND e.id IN :entry_ids
    """
    params: dict = {"user_id": user_id, "entry_ids": list(uniq)}
    if label_code:
        sql += " AND e.label_code = :label_code"
        params["label_code"] = label_code

    stmt = text(sql).bindparams(bindparam("entry_ids", expanding=True))
    result = db.execute(stmt, params)
    out: Dict[int, Dict] = {}
    for row in result.fetchall():
        out[int(row.entry_id)] = {
            "entry_id": int(row.entry_id),
            "user_id": row.user_id,
            "label_code": row.label_code,
            "label_name": row.label_name,
            "content": row.content,
            "created_at": row.created_at.isoformat() if getattr(row.created_at, "isoformat", None) else row.created_at,
        }
    return out


def search_similar_entries(
    db: Session,
    query_vector: List[float],
    user_id: int,
    top_k: int = 5,
    label_code: Optional[str] = None,
    min_score: float = RELEVANCE_THRESHOLD
) -> List[Dict]:
    """
    检索与查询向量最相似的记录（使用 FAISS）

    R5.2.3: FAISS returns IDs+scores, then one bulk hydrate for entries+labels.
    Preserves FAISS rank order, user_id isolation, and optional label_code filter.
    """
    try:
        hits = search_similar_entry_hits(
            db, query_vector, user_id, top_k=top_k, min_score=min_score
        )
        if not hits:
            return []
        by_id = bulk_get_entries_with_labels(
            db,
            [h["entry_id"] for h in hits],
            user_id=user_id,
            label_code=label_code,
        )
        enriched_results: List[Dict] = []
        for h in hits:
            eid = int(h["entry_id"])
            entry_info = by_id.get(eid)
            if not entry_info:
                continue
            enriched_results.append({
                "entry_id": eid,
                "relevance_score": float(h["relevance_score"]),
                "label_code": entry_info.get("label_code"),
                "label_name": entry_info.get("label_name"),
                "content": entry_info.get("content"),
                "created_at": entry_info.get("created_at"),
            })
        return enriched_results

    except Exception as e:
        logger.error(f"FAISS search failed for user {user_id}: {e}, falling back to NumPy")
        return _search_similar_entries_numpy(
            db, query_vector, user_id, top_k, label_code, min_score
        )


def _search_similar_entries_numpy(
    db: Session,
    query_vector: List[float],
    user_id: int,
    top_k: int = 5,
    label_code: Optional[str] = None,
    min_score: float = RELEVANCE_THRESHOLD
) -> List[Dict]:
    """
    检索与查询向量最相似的记录（NumPy fallback）

    当 FAISS 不可用时使用此方法
    """
    sql = """
        SELECT
            e.id as entry_id,
            e.user_id,
            e.label_code,
            e.content,
            e.created_at,
            emb.entry_type,
            emb.title_vector,
            emb.content_vector
        FROM entries e
        INNER JOIN embeddings emb ON e.id = emb.entry_id
        WHERE e.user_id = :user_id
    """
    params: dict = {"user_id": user_id}

    if label_code:
        sql += " AND e.label_code = :label_code"
        params["label_code"] = label_code

    result = db.execute(text(sql), params)
    rows = result.fetchall()

    if not rows:
        return []

    query_vec = np.array(query_vector, dtype=np.float32)
    query_norm = np.linalg.norm(query_vec)
    if query_norm == 0:
        return []

    title_vectors = []
    content_vectors = []
    meta = []

    def _parse_vector(raw, dim: int) -> np.ndarray:
        """解析向量数据，支持多种异常格式的容错处理"""
        if raw is None:
            return np.zeros(dim, dtype=np.float32)

        v = raw

        # 处理 numpy 字符串类型
        if hasattr(v, 'item'):
            try:
                v = v.item()
            except Exception:
                pass

        # 处理双重或多重序列化（尝试循环解析直到得到 list）
        max_iterations = 5
        for _ in range(max_iterations):
            if isinstance(v, list):
                break
            if isinstance(v, str):
                try:
                    v = json.loads(v)
                except json.JSONDecodeError:
                    break
            else:
                try:
                    v = str(v)
                except Exception:
                    return np.zeros(dim, dtype=np.float32)

        # 最终检查：确保 v 是 list
        if not isinstance(v, list):
            if isinstance(v, str) and v.startswith('['):
                try:
                    import ast
                    v = ast.literal_eval(v)
                except Exception:
                    pass

        if not isinstance(v, list):
            return np.zeros(dim, dtype=np.float32)

        return np.array(v, dtype=np.float32)

    for row in rows:
        title_vectors.append(_parse_vector(row.title_vector, len(query_vector)))
        content_vectors.append(_parse_vector(row.content_vector, len(query_vector)))
        meta.append({
            "entry_id": row.entry_id,
            "entry_type": row.entry_type,
            "content": row.content,
            "created_at": row.created_at,
            "label_code": row.label_code,
        })

    title_mat = np.vstack(title_vectors)
    content_mat = np.vstack(content_vectors)

    title_norms = np.linalg.norm(title_mat, axis=1)
    content_norms = np.linalg.norm(content_mat, axis=1)

    title_norms[title_norms == 0] = 1.0
    content_norms[content_norms == 0] = 1.0

    title_sims = title_mat @ query_vec / (title_norms * query_norm)
    content_sims = content_mat @ query_vec / (content_norms * query_norm)

    combined_scores = title_sims * 0.3 + content_sims * 0.7

    above_threshold = np.where(combined_scores >= min_score)[0]
    if len(above_threshold) == 0:
        k = min(top_k, len(combined_scores))
        top_indices = np.argsort(combined_scores)[-k:][::-1]
    else:
        sorted_above = above_threshold[np.argsort(combined_scores[above_threshold])[::-1]]
        top_indices = sorted_above[:top_k]

    results = []
    for idx in top_indices:
        score = float(combined_scores[idx])
        if score < min_score and len(above_threshold) > 0:
            continue
        item = meta[idx].copy()
        item["relevance_score"] = score
        results.append(item)

    return results


def get_entry_with_label(db: Session, entry_id: int, user_id: Optional[int] = None) -> Optional[Dict]:
    """获取单条记录的详细信息（包含标签名）"""
    sql = """
        SELECT
            e.id as entry_id,
            e.user_id,
            e.label_code,
            e.content,
            e.created_at,
            l.name as label_name
        FROM entries e
        LEFT JOIN entry_labels l ON e.label_code = l.code
        WHERE e.id = :entry_id
    """
    params = {"entry_id": entry_id}
    if user_id is not None:
        sql += " AND e.user_id = :user_id"
        params["user_id"] = user_id

    result = db.execute(text(sql), params)
    row = result.fetchone()

    if not row:
        return None

    return {
        "entry_id": row.entry_id,
        "user_id": row.user_id,
        "label_code": row.label_code,
        "label_name": row.label_name,
        "content": row.content,
        "created_at": row.created_at
    }


def add_entry_to_index(
    user_id: int,
    entry_id: int,
    title_vec: List[float],
    content_vec: List[float]
):
    """
    添加单条记录到 FAISS 索引

    确保新记录立即可被 AI 检索到
    """
    try:
        manager = _get_index_manager()

        # 确保索引已初始化
        user_index = manager.get_index(user_id)

        # 添加向量到 FAISS 内存索引
        user_index.add_vector(entry_id, title_vec, content_vec)

        # 标记为 dirty，触发持久化
        manager._dirty_indexes.add(user_id)

        logger.debug(f"Added entry {entry_id} to index for user {user_id}, marked dirty")
    except Exception as e:
        logger.error(f"Failed to add entry {entry_id} to index: {e}")


def remove_entry_from_index(user_id: int, entry_id: int):
    """从用户索引移除向量（软删除）"""
    try:
        manager = _get_index_manager()
        manager.remove_vector(user_id, entry_id)
        logger.debug(f"Removed entry {entry_id} from index for user {user_id}")
    except Exception as e:
        logger.error(f"Failed to remove entry {entry_id} from index: {e}")


def rebuild_user_index_from_db(user_id: int, db: Session):
    """
    从数据库已持久化的 embeddings 重建 FAISS 索引。

    R5.2.2：不得在此路径批量重算记录正文向量；缺失向量只能走后台 backfill。
    """
    logger.info(f"Rebuilding FAISS index for user {user_id} from stored embeddings")
    manager = _get_index_manager()
    manager.cache.remove(user_id)
    manager.rebuild_from_db(db, user_id)


def initialize_user_index(user_id: int, db: Session):
    """
    初始化用户索引（确保与数据库一致）

    在用户首次进行向量检索时调用
    增强检查：确保 FAISS 索引与数据库 embeddings 表一致
    """
    manager = _get_index_manager()

    # 1. 获取或创建索引
    user_index = manager.get_index(user_id)

    # 2. 检查是否需要从数据库重建
    if manager.needs_rebuild(db, user_id):
        logger.warning(f"Index for user {user_id} needs rebuild, rebuilding from database...")
        manager.rebuild_from_db(db, user_id)
        return

    # 索引已存在且一致，无需额外操作
    logger.debug(f"Index for user {user_id} is already in sync")


def get_index_stats() -> Dict:
    """获取索引统计信息"""
    try:
        manager = _get_index_manager()
        return manager.get_stats()
    except Exception as e:
        logger.error(f"Failed to get index stats: {e}")
        return {"error": str(e)}


def get_user_index_status(user_id: int) -> Optional[Dict]:
    """获取用户索引状态"""
    try:
        manager = _get_index_manager()
        return manager.get_user_index_status(user_id)
    except Exception as e:
        logger.error(f"Failed to get index status for user {user_id}: {e}")
        return None


def persist_all_indexes():
    """持久化所有脏索引"""
    try:
        manager = _get_index_manager()
        manager.persist_dirty_indexes()
        logger.info("Persisted all dirty indexes")
    except Exception as e:
        logger.error(f"Failed to persist indexes: {e}")
