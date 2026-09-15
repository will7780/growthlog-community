"""
向量存储接口

定义向量存储后端的抽象接口，预留云端存储扩展能力。
"""
from abc import ABC, abstractmethod
from typing import Optional


class VectorStorageBackend(ABC):
    """
    向量存储后端抽象接口

    所有存储后端（本地磁盘、云端存储等）必须实现此接口
    """

    @abstractmethod
    def save_index(
        self,
        user_id: int,
        index_data: bytes,
        id_mapping: dict,
        metadata: dict
    ) -> bool:
        """
        保存索引

        Args:
            user_id: 用户ID
            index_data: FAISS 索引二进制数据
            id_mapping: {faiss_internal_id: entry_id} 映射
            metadata: 元数据 {"vector_count": int, "last_updated": str, ...}

        Returns:
            bool: 保存是否成功
        """
        pass

    @abstractmethod
    def load_index(self, user_id: int) -> Optional[tuple[bytes, dict, dict]]:
        """
        加载索引

        Args:
            user_id: 用户ID

        Returns:
            (index_data, id_mapping, metadata) 或 None
        """
        pass

    @abstractmethod
    def delete_index(self, user_id: int) -> bool:
        """
        删除索引

        Args:
            user_id: 用户ID

        Returns:
            bool: 删除是否成功
        """
        pass

    @abstractmethod
    def exists(self, user_id: int) -> bool:
        """
        检查索引是否存在

        Args:
            user_id: 用户ID

        Returns:
            bool: 是否存在
        """
        pass

    @abstractmethod
    def list_users(self) -> list[int]:
        """
        列出所有索引的用户ID

        Returns:
            list[int]: 用户ID列表
        """
        pass


# ========== 云端存储配置 ==========

class CloudStorageConfig:
    """云端存储配置基类"""

    def __init__(
        self,
        provider: str,  # "s3", "oss", "minio"
        endpoint: str = "",
        bucket: str = "",
        access_key: str = "",
        secret_key: str = "",
        region: str = "us-east-1",
        **kwargs
    ):
        self.provider = provider
        self.endpoint = endpoint
        self.bucket = bucket
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region
        self.extra = kwargs


# ========== 云端存储实现预留 ==========

# 未来可以实现以下云端存储后端：
#
# class S3StorageBackend(VectorStorageBackend):
#     """AWS S3 / MinIO 存储后端"""
#     pass
#
# class OSSStorageBackend(VectorStorageBackend):
#     """阿里云 OSS 存储后端"""
#     pass
#
# class GCSStorageBackend(VectorStorageBackend):
#     """Google Cloud Storage 存储后端"""
#     pass
#
# class AzureBlobStorageBackend(VectorStorageBackend):
#     """Azure Blob Storage 存储后端"""
#     pass
