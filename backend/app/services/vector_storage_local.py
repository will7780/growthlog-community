"""
本地磁盘存储后端

将向量索引存储到本地文件系统
采用原子性保存：先写临时目录，验证通过后移动到正式目录
"""
import json
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from app.services.vector_index import VectorStorageBackend

logger = logging.getLogger(__name__)


class LocalStorageBackend(VectorStorageBackend):
    """
    本地磁盘存储后端

    存储结构：
    {storage_path}/
    └── user_{user_id}/
        ├── index.faiss      # FAISS 索引二进制文件
        ├── id_mapping.json  # entry_id 映射
        └── meta.json        # 元数据

    原子性保存：
    1. 先写入临时目录 temp_xxx/
    2. 验证所有文件完整（index.faiss > 0, JSON 可解析）
    3. 原子性移动到正式目录
    """

    # 最小有效 FAISS 文件大小（通常 8字节以上）
    MIN_FAISS_SIZE = 8

    def __init__(self, base_path: str = "vector_indexes"):
        self.base_path = Path(base_path)
        self._ensure_base_path()

    def _ensure_base_path(self):
        """确保基础目录存在"""
        self.base_path.mkdir(parents=True, exist_ok=True)

    def _user_path(self, user_id: int) -> Path:
        """获取用户索引目录"""
        return self.base_path / f"user_{user_id}"

    def _temp_path(self, user_id: int) -> Path:
        """获取临时目录路径"""
        return self.base_path / f"temp_{user_id}_{id(self)}"

    def _index_path(self, user_id: int) -> Path:
        """获取索引文件路径"""
        return self._user_path(user_id) / "index.faiss"

    def _mapping_path(self, user_id: int) -> Path:
        """获取映射文件路径"""
        return self._user_path(user_id) / "id_mapping.json"

    def _meta_path(self, user_id: int) -> Path:
        """获取元数据文件路径"""
        return self._user_path(user_id) / "meta.json"

    def save_index(
        self,
        user_id: int,
        index_data: bytes,
        id_mapping: dict,
        metadata: dict
    ) -> bool:
        """
        保存索引到磁盘（原子性保存）

        Args:
            user_id: 用户ID
            index_data: FAISS 索引二进制数据
            id_mapping: {faiss_internal_id: entry_id} 映射
            metadata: 元数据

        Returns:
            bool: 保存是否成功
        """
        temp_path = self._temp_path(user_id)
        user_path = self._user_path(user_id)

        try:
            # 1. 创建临时目录
            if temp_path.exists():
                shutil.rmtree(temp_path)
            temp_path.mkdir(parents=True, exist_ok=True)

            # 2. 写入临时目录
            # 2.1 写入 FAISS 索引
            temp_index = temp_path / "index.faiss"
            with open(temp_index, 'wb') as f:
                f.write(index_data)

            # 2.2 写入 id_mapping
            temp_mapping = temp_path / "id_mapping.json"
            with open(temp_mapping, 'w', encoding='utf-8') as f:
                json.dump(id_mapping, f, ensure_ascii=False, indent=2)

            # 2.3 写入 metadata
            temp_meta = temp_path / "meta.json"
            with open(temp_meta, 'w', encoding='utf-8') as f:
                json.dump(metadata, f, ensure_ascii=False, indent=2)

            # 3. 验证所有文件
            if not self._verify_temp_index(temp_path, len(index_data)):
                logger.error(f"Verification failed for user {user_id}")
                shutil.rmtree(temp_path)
                return False

            # 4. 原子性移动到正式目录
            if user_path.exists():
                shutil.rmtree(user_path)
            shutil.move(str(temp_path), str(user_path))

            logger.info(f"Saved index for user {user_id} to {user_path}")
            return True

        except Exception as e:
            logger.error(f"Failed to save index for user {user_id}: {e}")
            if temp_path.exists():
                shutil.rmtree(temp_path)
            return False

    def _verify_temp_index(self, temp_path: Path, expected_index_size: int) -> bool:
        """
        验证临时目录中的文件是否完整有效

        Returns:
            bool: 验证是否通过
        """
        try:
            # 验证 index.faiss
            index_file = temp_path / "index.faiss"
            if not index_file.exists():
                logger.error(f"index.faiss not found in temp directory")
                return False
            actual_size = index_file.stat().st_size
            if actual_size < self.MIN_FAISS_SIZE:
                logger.error(f"index.faiss size {actual_size} is too small, expected > {self.MIN_FAISS_SIZE}")
                return False
            if actual_size != expected_index_size:
                logger.error(f"index.faiss size mismatch: {actual_size} != {expected_index_size}")
                return False

            # 验证 id_mapping.json 可解析
            mapping_file = temp_path / "id_mapping.json"
            with open(mapping_file, 'r', encoding='utf-8') as f:
                json.load(f)  # 尝试解析，失败会抛异常

            # 验证 meta.json 可解析
            meta_file = temp_path / "meta.json"
            with open(meta_file, 'r', encoding='utf-8') as f:
                json.load(f)

            return True

        except json.JSONDecodeError as e:
            logger.error(f"JSON decode error during verification: {e}")
            return False
        except Exception as e:
            logger.error(f"Verification error: {e}")
            return False

    def load_index(self, user_id: int) -> Optional[tuple[bytes, dict, dict]]:
        """
        从磁盘加载索引

        Args:
            user_id: 用户ID

        Returns:
            (index_data, id_mapping, metadata) 或 None
        """
        index_path = self._index_path(user_id)
        mapping_path = self._mapping_path(user_id)
        meta_path = self._meta_path(user_id)

        try:
            # 检查文件是否存在
            if not all(p.exists() for p in [index_path, mapping_path, meta_path]):
                return None

            # 读取 FAISS 索引
            with open(index_path, 'rb') as f:
                index_data = f.read()

            # 读取 id_mapping
            with open(mapping_path, 'r', encoding='utf-8') as f:
                id_mapping = json.load(f)

            # 读取 metadata
            with open(meta_path, 'r', encoding='utf-8') as f:
                metadata = json.load(f)

            logger.debug(f"Loaded index for user {user_id} from {self._user_path(user_id)}")
            return (index_data, id_mapping, metadata)

        except Exception as e:
            logger.error(f"Failed to load index for user {user_id}: {e}")
            return None

    def delete_index(self, user_id: int) -> bool:
        """
        删除用户索引

        Args:
            user_id: 用户ID

        Returns:
            bool: 删除是否成功
        """
        import shutil

        user_path = self._user_path(user_id)

        try:
            if user_path.exists():
                shutil.rmtree(user_path)
                logger.info(f"Deleted index for user {user_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to delete index for user {user_id}: {e}")
            return False

    def exists(self, user_id: int) -> bool:
        """检查索引是否存在"""
        return self._index_path(user_id).exists()

    def list_users(self) -> list[int]:
        """列出所有索引的用户ID"""
        user_ids = []

        try:
            for item in self.base_path.iterdir():
                if item.is_dir() and item.name.startswith("user_"):
                    try:
                        user_id = int(item.name[5:])  # "user_123" -> 123
                        # 验证是否是有效的索引目录
                        if self._index_path(user_id).exists():
                            user_ids.append(user_id)
                    except ValueError:
                        continue

        except Exception as e:
            logger.error(f"Failed to list users: {e}")

        return sorted(user_ids)

    def _cleanup_failed_save(self, user_id: int):
        """清理保存失败的文件"""
        import shutil

        user_path = self._user_path(user_id)
        try:
            if user_path.exists():
                shutil.rmtree(user_path)
        except Exception:
            pass

    def get_storage_size(self, user_id: int) -> int:
        """获取用户索引存储大小（字节）"""
        total_size = 0
        user_path = self._user_path(user_id)

        if user_path.exists():
            for item in user_path.rglob('*'):
                if item.is_file():
                    total_size += item.stat().st_size

        return total_size

    def get_total_size(self) -> int:
        """获取所有索引的总存储大小（字节）"""
        total_size = 0

        try:
            for item in self.base_path.rglob('*'):
                if item.is_file():
                    total_size += item.stat().st_size
        except Exception as e:
            logger.error(f"Failed to calculate total size: {e}")

        return total_size
