"""
向量索引管理模块

使用 FAISS 实现高效的向量检索，支持：
- 每个用户独立的向量索引
- LRU 缓存加速访问
- 磁盘持久化存储
- 预留云端存储接口
"""
import json
import logging
import os
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

import faiss
import numpy as np

logger = logging.getLogger(__name__)


def _faiss_index_to_bytes(index) -> bytes:
    """Serialize FAISS index to plain bytes (NumPy buffer → bytes)."""
    raw = faiss.serialize_index(index)
    return np.asarray(raw, dtype=np.uint8).tobytes()


def _coerce_faiss_buffer(index_data) -> np.ndarray:
    """
    Deserialize input may be bytes (from disk) or NumPy buffer (in-memory).
    Always coerce to contiguous uint8 ndarray for faiss.deserialize_index.
    """
    if isinstance(index_data, np.ndarray):
        arr = np.ascontiguousarray(index_data, dtype=np.uint8)
    elif isinstance(index_data, (bytes, bytearray, memoryview)):
        arr = np.frombuffer(index_data, dtype=np.uint8)
    else:
        arr = np.asarray(index_data, dtype=np.uint8)
    if not arr.flags["C_CONTIGUOUS"]:
        arr = np.ascontiguousarray(arr)
    return arr


# ========== 数据结构 ==========

@dataclass
class UserVectorIndex:
    """单个用户的向量索引"""
    user_id: int
    index: faiss.IndexFlatIP
    id_mapping: dict[int, int]  # faiss internal id (int) -> entry_id (int)
    reverse_mapping: dict[int, int]  # entry_id -> faiss internal id
    deleted_ids: set[int] = field(default_factory=set)
    vector_count: int = 0
    last_updated: datetime = field(default_factory=datetime.now)
    is_dirty: bool = False

    def add_vector(self, entry_id: int, title_vec: list, content_vec: list):
        """添加向量到索引（组合标题和内容向量）"""
        # 组合标题和内容向量（加权）
        combined = np.array(title_vec, dtype=np.float32) * 0.3 + \
                   np.array(content_vec, dtype=np.float32) * 0.7
        # L2 归一化（FAISS IndexFlatIP 需要）
        faiss.normalize_L2(combined.reshape(1, -1))

        # 添加到 FAISS 索引
        faiss_id = len(self.id_mapping)
        self.index.add(combined.reshape(1, -1))

        # 更新映射
        self.id_mapping[faiss_id] = entry_id
        self.reverse_mapping[entry_id] = faiss_id
        self.vector_count += 1
        self.is_dirty = True
        self.last_updated = datetime.now()

    def remove_vector(self, entry_id: int):
        """标记向量为已删除（软删除）"""
        if entry_id in self.reverse_mapping:
            self.deleted_ids.add(entry_id)
            self.is_dirty = True
            self.last_updated = datetime.now()

    def search(self, query_vec: list, k: int = 5, min_score: float = 0.35) -> list[dict]:
        """检索相似向量，返回 [(entry_id, score), ...]"""
        query_np = np.array([query_vec], dtype=np.float32)
        faiss.normalize_L2(query_np)

        # 多取一些结果用于过滤
        search_k = min(k * 3, self.vector_count)
        if search_k <= 0:
            return []

        distances, indices = self.index.search(query_np, search_k)

        results = []
        for dist, idx in zip(distances[0], indices[0]):
            if idx < 0:  # FAISS 返回 -1 表示无效
                continue

            entry_id = self.id_mapping.get(int(idx))
            if entry_id is None:
                continue

            # 过滤已删除的
            if entry_id in self.deleted_ids:
                continue

            # 过滤低分
            if dist < min_score:
                continue

            results.append({
                "entry_id": entry_id,
                "relevance_score": float(dist)
            })

            if len(results) >= k:
                break

        return results

    def to_dict(self) -> dict:
        """序列化为字典"""
        return {
            "user_id": self.user_id,
            "vector_count": self.vector_count,
            "last_updated": self.last_updated.isoformat(),
            "deleted_count": len(self.deleted_ids)
        }


# ========== LRU Cache ==========

class LRUCache:
    """简单的 LRU 缓存"""

    def __init__(self, max_size: int):
        self.max_size = max_size
        self._cache: dict[int, UserVectorIndex] = {}
        self._access_order: list[int] = []
        self._lock = threading.Lock()

    def get(self, key: int) -> Optional[UserVectorIndex]:
        """获取缓存项"""
        with self._lock:
            if key in self._cache:
                # 移到末尾（最近使用）
                self._access_order.remove(key)
                self._access_order.append(key)
                return self._cache[key]
            return None

    def put(self, key: int, value: UserVectorIndex):
        """放入缓存"""
        with self._lock:
            if key in self._cache:
                self._access_order.remove(key)
            elif len(self._cache) >= self.max_size:
                # 淘汰最旧的
                oldest = self._access_order.pop(0)
                del self._cache[oldest]
                logger.debug(f"LRU cache evicted user {oldest}")

            self._cache[key] = value
            self._access_order.append(key)

    def remove(self, key: int):
        """移除缓存项"""
        with self._lock:
            if key in self._cache:
                del self._cache[key]
                self._access_order.remove(key)

    def get_stats(self) -> dict:
        """获取缓存统计"""
        with self._lock:
            return {
                "size": len(self._cache),
                "max_size": self.max_size,
                "users": list(self._cache.keys())
            }

    def get_dirty_indexes(self) -> list[UserVectorIndex]:
        """获取所有脏索引"""
        with self._lock:
            return [idx for idx in self._cache.values() if idx.is_dirty]


# ========== 存储后端接口 ==========

class VectorStorageBackend(ABC):
    """向量存储后端抽象"""

    @abstractmethod
    def save_index(self, user_id: int, index_data: bytes, id_mapping: dict, metadata: dict) -> bool:
        """保存索引"""
        pass

    @abstractmethod
    def load_index(self, user_id: int) -> Optional[tuple[bytes, dict, dict]]:
        """
        加载索引
        返回: (index_data, id_mapping, metadata) 或 None
        """
        pass

    @abstractmethod
    def delete_index(self, user_id: int) -> bool:
        """删除索引"""
        pass

    @abstractmethod
    def exists(self, user_id: int) -> bool:
        """检查索引是否存在"""
        pass

    @abstractmethod
    def list_users(self) -> list[int]:
        """列出所有索引的用户ID"""
        pass


# ========== VectorIndexManager ==========

class VectorIndexManager:
    """
    向量索引管理器（单例）

    负责：
    - 管理用户的向量索引
    - LRU 缓存加速
    - 磁盘持久化
    """

    _instance: Optional['VectorIndexManager'] = None
    _lock = threading.Lock()

    def __init__(
        self,
        storage_path: str | None = None,
        cache_size: int = 100,
        index_dim: int = 768,
        storage_backend: Optional[VectorStorageBackend] = None
    ):
        # Optional isolate override for local E2E (never required in production).
        resolved = storage_path or os.environ.get("GROWTHLOG_VECTOR_INDEX_DIR") or "vector_indexes"
        self.storage_path = Path(resolved)
        self.cache_size = cache_size
        self.index_dim = index_dim
        if storage_backend is None:
            # Lazy import avoids circular import with vector_storage_local.
            from app.services.vector_storage_local import LocalStorageBackend

            storage_backend = LocalStorageBackend(base_path=str(self.storage_path))
        self.storage = storage_backend

        self.cache = LRUCache(max_size=cache_size)
        self._dirty_indexes: set[int] = set()  # 待持久化的用户ID

        # 持久化线程
        self._persist_thread: Optional[threading.Thread] = None
        self._persist_interval = 60  # 每60秒检查一次
        self._running = True

    @classmethod
    def get_instance(cls, **kwargs) -> 'VectorIndexManager':
        """获取单例"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(**kwargs)
                    cls._instance._start_persist_thread()
        return cls._instance

    @classmethod
    def reset_instance(cls):
        """重置单例（用于测试）"""
        global _manager
        with cls._lock:
            if cls._instance is not None:
                cls._instance.shutdown()
            cls._instance = None
        _manager = None
        try:
            from app.services import vector_search as _vs

            _vs._index_manager = None
        except Exception:
            pass

    def _start_persist_thread(self):
        """启动持久化线程"""
        self._persist_thread = threading.Thread(target=self._persist_loop, daemon=True)
        self._persist_thread.start()

    def _persist_loop(self):
        """持久化循环（增强版：带验证）"""
        while self._running:
            time.sleep(self._persist_interval)
            self.persist_dirty_indexes()

            # 持久化后验证（检查脏索引是否已成功持久化）
            for user_id in list(self._dirty_indexes):
                user_index = self.cache.get(user_id)
                if user_index and not user_index.is_dirty:
                    # 已标记为不脏，验证持久化是否成功
                    if not self._verify_persistence(user_id, user_index):
                        logger.error(f"Persistence verification failed for user {user_id}, will retry")
                        user_index.is_dirty = True  # 重新持久化

    def shutdown(self):
        """关闭时保存所有脏索引"""
        self._running = False
        if self._persist_thread:
            self._persist_thread.join(timeout=5)
        self.persist_dirty_indexes()

    def get_index(self, user_id: int) -> UserVectorIndex:
        """
        获取用户索引（自动处理加载/缓存）
        """
        # 1. 尝试从缓存获取
        user_index = self.cache.get(user_id)
        if user_index is not None:
            return user_index

        # 2. 尝试从存储加载
        user_index = self._load_from_storage(user_id)
        if user_index is not None:
            self.cache.put(user_id, user_index)
            return user_index

        # 3. 创建新索引
        user_index = self._create_empty_index(user_id)
        self.cache.put(user_id, user_index)
        return user_index

    def _create_empty_index(self, user_id: int) -> UserVectorIndex:
        """创建空索引"""
        index = faiss.IndexFlatIP(self.index_dim)
        return UserVectorIndex(
            user_id=user_id,
            index=index,
            id_mapping={},
            reverse_mapping={},
            deleted_ids=set(),
            vector_count=0,
            last_updated=datetime.now(),
            is_dirty=False
        )

    def _load_from_storage(self, user_id: int) -> Optional[UserVectorIndex]:
        """从存储加载索引"""
        if self.storage is None:
            return None

        try:
            data = self.storage.load_index(user_id)
            if data is None:
                return None

            index_data, id_mapping_dict, metadata = data

            # 反序列化 id_mapping
            id_mapping = {int(k): v for k, v in id_mapping_dict.items()}
            reverse_mapping = {v: k for k, v in id_mapping.items()}

            # 反序列化 deleted_ids
            deleted_ids = set(metadata.get("deleted_ids", []))

            # 加载 FAISS 索引（bytes ↔ NumPy uint8 buffer 对称）
            index = faiss.deserialize_index(_coerce_faiss_buffer(index_data))

            user_index = UserVectorIndex(
                user_id=user_id,
                index=index,
                id_mapping=id_mapping,
                reverse_mapping=reverse_mapping,
                deleted_ids=deleted_ids,
                vector_count=metadata.get("vector_count", len(id_mapping)),
                last_updated=datetime.fromisoformat(metadata["last_updated"]) if "last_updated" in metadata else datetime.now(),
                is_dirty=False
            )

            logger.info(f"Loaded index for user {user_id}: {user_index.vector_count} vectors")
            return user_index

        except Exception as e:
            logger.error(f"Failed to load index for user {user_id}: {e}")
            return None

    def add_vector(self, user_id: int, entry_id: int, title_vec: list, content_vec: list):
        """添加向量到用户索引"""
        user_index = self.get_index(user_id)
        user_index.add_vector(entry_id, title_vec, content_vec)
        self._dirty_indexes.add(user_id)

    def remove_vector(self, user_id: int, entry_id: int):
        """标记向量为已删除"""
        user_index = self.get_index(user_id)
        user_index.remove_vector(entry_id)
        self._dirty_indexes.add(user_id)

        # 检查是否需要重建
        if len(user_index.deleted_ids) / max(user_index.vector_count, 1) > 0.2:
            logger.info(f"User {user_id} index has {len(user_index.deleted_ids)} deleted, triggering rebuild")
            self.rebuild_index(user_id)

    def search(
        self,
        user_id: int,
        query_vector: list,
        k: int = 5,
        min_score: float = 0.35
    ) -> list[dict]:
        """检索相似记录"""
        user_index = self.get_index(user_id)
        return user_index.search(query_vector, k, min_score)

    def rebuild_index(self, user_id: int):
        """从数据库重建索引（短生命周期只读 Session；失败不得清 dirty）。"""
        from app.database import SessionLocal
        from app.services.vector_search import rebuild_user_index_from_db

        db = SessionLocal()
        try:
            rebuild_user_index_from_db(user_id, db)
            # 重新加载 — only after successful rebuild
            self.cache.remove(user_id)
            self._dirty_indexes.discard(user_id)
            logger.info(f"Rebuilt index for user {user_id}")
        except Exception as e:
            # Keep dirty so a later search/persist can retry; never pretend success
            # (do not discard dirty / do not clear cache on failure).
            logger.error(
                "Failed to rebuild index for user %s: %s",
                user_id,
                type(e).__name__,
            )
        finally:
            db.close()

    def persist_index(self, user_id: int) -> bool:
        """持久化单个用户索引到存储"""
        if self.storage is None:
            return False

        user_index = self.cache.get(user_id)
        if user_index is None:
            return False

        if not user_index.is_dirty:
            return True

        try:
            # 序列化为稳定 bytes（serialize_index 可能返回 NumPy buffer）
            index_data = _faiss_index_to_bytes(user_index.index)
            id_mapping_dict = {str(k): v for k, v in user_index.id_mapping.items()}
            metadata = {
                "vector_count": user_index.vector_count,
                "last_updated": user_index.last_updated.isoformat(),
                "deleted_ids": list(user_index.deleted_ids),
                "version": 1
            }

            # 保存
            success = self.storage.save_index(
                user_id, index_data, id_mapping_dict, metadata
            )

            if success:
                user_index.is_dirty = False
                self._dirty_indexes.discard(user_id)
                logger.debug(f"Persisted index for user {user_id}")

            return success

        except Exception as e:
            logger.error(f"Failed to persist index for user {user_id}: {e}")
            return False

    def persist_dirty_indexes(self):
        """持久化所有脏索引"""
        dirty_users = list(self._dirty_indexes)
        for user_id in dirty_users:
            self.persist_index(user_id)

    def needs_rebuild(self, db, user_id: int) -> bool:
        """
        检查是否需要从数据库重建索引

        检查条件：
        1. FAISS 向量数 != 数据库该用户的 embeddings 记录数
        2. 或 FAISS 文件不存在
        """
        # 检查 FAISS 文件是否存在
        if self.storage is None or not self.storage.exists(user_id):
            logger.info(f"User {user_id} index file does not exist, needs rebuild")
            return True

        # 获取 FAISS 内存中的向量数
        user_index = self.cache.get(user_id)
        faiss_count = user_index.vector_count if user_index else 0

        # 获取数据库中该用户的 embeddings 数量（通过 entry_id 关联 entries 表）
        from app.models.entry import Entry
        from app.models.embedding import Embedding
        db_count = db.query(Embedding).join(Entry, Embedding.entry_id == Entry.id).filter(
            Entry.user_id == user_id,
            Embedding.entry_type == "main"
        ).count()

        if faiss_count != db_count:
            logger.info(f"User {user_id} vector count mismatch: FAISS={faiss_count}, DB={db_count}, needs rebuild")
            return True

        return False

    def rebuild_from_db(self, db, user_id: int):
        """
        从数据库重建 FAISS 索引

        流程：
        1. 清空当前索引
        2. 从 embeddings 表加载所有向量
        3. 重新添加到 FAISS
        4. 持久化到磁盘
        """
        from app.models.embedding import Embedding

        logger.info(f"Rebuilding FAISS index for user {user_id} from stored embeddings...")

        # 1. 获取或创建索引
        user_index = self.get_index(user_id)

        # 2. 清空当前索引（重建）
        index = faiss.IndexFlatIP(self.index_dim)
        user_index.index = index
        user_index.id_mapping = {}
        user_index.reverse_mapping = {}
        user_index.deleted_ids = set()
        user_index.vector_count = 0

        # 3. 从数据库加载所有 embeddings（通过 entry_id 关联 entries 表获取 user_id）
        from app.models.entry import Entry
        embeddings = db.query(Embedding).join(Entry, Embedding.entry_id == Entry.id).filter(
            Entry.user_id == user_id,
            Embedding.entry_type == "main"
        ).all()

        if not embeddings:
            logger.info(f"No embeddings found for user {user_id}, index remains empty")
            user_index.is_dirty = True
            self.persist_index(user_id)
            return

        # 4. 逐个添加向量到 FAISS
        for emb in embeddings:
            try:
                # 解析存储的向量
                import ast
                title_vec = ast.literal_eval(emb.title_vector) if isinstance(emb.title_vector, str) else emb.title_vector
                content_vec = ast.literal_eval(emb.content_vector) if isinstance(emb.content_vector, str) else emb.content_vector

                # 添加到 FAISS 索引
                faiss_id = user_index.vector_count
                combined = np.array(title_vec, dtype=np.float32) * 0.3 + \
                           np.array(content_vec, dtype=np.float32) * 0.7
                faiss.normalize_L2(combined.reshape(1, -1))
                user_index.index.add(combined.reshape(1, -1))

                # 更新映射
                user_index.id_mapping[faiss_id] = emb.entry_id
                user_index.reverse_mapping[emb.entry_id] = faiss_id
                user_index.vector_count += 1

            except Exception as e:
                logger.warning(f"Failed to add embedding {emb.id} for entry {emb.entry_id}: {e}")
                continue

        user_index.is_dirty = True
        user_index.last_updated = datetime.now()

        # 5. 持久化到磁盘
        self.persist_index(user_id)

        logger.info(f"Rebuilt index for user {user_id}: {user_index.vector_count} vectors added")

    def _verify_persistence(self, user_id: int, user_index: UserVectorIndex) -> bool:
        """
        验证持久化是否成功

        验证：
        1. 持久化后的文件存在
        2. index.faiss 大小 > 0
        3. 加载后向量数一致
        """
        if self.storage is None:
            return True

        try:
            # 检查文件是否存在
            index_path = self.storage._index_path(user_id) if hasattr(self.storage, '_index_path') else None
            if index_path and not index_path.exists():
                logger.error(f"Verification failed: index.faiss does not exist for user {user_id}")
                return False

            # 重新加载验证
            reloaded = self._load_from_storage(user_id)
            if reloaded is None:
                logger.error(f"Verification failed: cannot reload index for user {user_id}")
                return False

            if reloaded.vector_count != user_index.vector_count:
                logger.error(f"Verification failed: vector count mismatch after reload: {reloaded.vector_count} != {user_index.vector_count}")
                return False

            if len(reloaded.id_mapping) != len(user_index.id_mapping):
                logger.error(f"Verification failed: id_mapping size mismatch after reload: {len(reloaded.id_mapping)} != {len(user_index.id_mapping)}")
                return False

            logger.debug(f"Persistence verification passed for user {user_id}")
            return True

        except Exception as e:
            logger.error(f"Verification error for user {user_id}: {e}")
            return False

    def get_stats(self) -> dict:
        """获取统计信息"""
        cache_stats = self.cache.get_stats()
        dirty_count = len(self._dirty_indexes)

        total_vectors = 0
        for idx in self.cache.get_dirty_indexes():
            total_vectors += idx.vector_count

        return {
            "cached_users": cache_stats["size"],
            "cache_max": cache_stats["max_size"],
            "dirty_indexes": dirty_count,
            "total_vectors": total_vectors,
            "storage_backend": type(self.storage).__name__ if self.storage else "None"
        }

    def get_user_index_status(self, user_id: int) -> Optional[dict]:
        """获取用户索引状态"""
        user_index = self.cache.get(user_id)
        if user_index is None:
            # 尝试从存储加载
            user_index = self._load_from_storage(user_id)
            if user_index is None:
                return None

        return user_index.to_dict()

    def load_active_users(self, user_ids: list[int]):
        """预加载活跃用户索引到缓存"""
        for user_id in user_ids:
            try:
                self.get_index(user_id)
            except Exception as e:
                logger.warning(f"Failed to preload index for user {user_id}: {e}")


# ========== 便捷函数 ==========

_manager: Optional[VectorIndexManager] = None


def get_index_manager() -> VectorIndexManager:
    """获取索引管理器单例（尊重 GROWTHLOG_VECTOR_INDEX_DIR）"""
    global _manager
    if VectorIndexManager._instance is None:
        resolved = (os.environ.get("GROWTHLOG_VECTOR_INDEX_DIR") or "").strip() or "vector_indexes"
        from app.services.vector_storage_local import LocalStorageBackend

        _manager = VectorIndexManager.get_instance(
            storage_path=resolved,
            storage_backend=LocalStorageBackend(base_path=resolved),
        )
    elif _manager is None or _manager is not VectorIndexManager._instance:
        _manager = VectorIndexManager._instance
    return _manager


def add_to_index(user_id: int, entry_id: int, title_vec: list, content_vec: list):
    """添加向量到索引（便捷函数）"""
    manager = get_index_manager()
    manager.add_vector(user_id, entry_id, title_vec, content_vec)


def search_index(user_id: int, query_vec: list, k: int = 5, min_score: float = 0.35) -> list[dict]:
    """搜索索引（便捷函数）"""
    manager = get_index_manager()
    return manager.search(user_id, query_vec, k, min_score)


def remove_from_index(user_id: int, entry_id: int):
    """从索引移除（便捷函数）"""
    manager = get_index_manager()
    manager.remove_vector(user_id, entry_id)
