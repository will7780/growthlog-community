"""
启动同步服务
后端启动时自动同步所有用户的向量数据到 FAISS 索引
"""
import logging

from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.models import Entry, Embedding, User
from app.services.embedding import generate_title_content_embedding
from app.services.vector_index import get_index_manager

logger = logging.getLogger(__name__)


def sync_all_users_embeddings():
    """
    遍历所有用户，检查并补全缺失的向量
    仅补全缺失部分，已存在的向量不会被覆盖
    """
    db = SessionLocal()
    try:
        # 获取所有用户
        users = db.query(User).filter(User.is_active == True).all()
        logger.info(f"[Startup Sync] 找到 {len(users)} 个用户，开始同步向量...")

        for user in users:
            sync_user_embeddings(user.id, db)

        logger.info(f"[Startup Sync] 所有用户向量同步完成")
    except Exception as e:
        logger.error(f"[Startup Sync] 向量同步失败: {e}")
    finally:
        db.close()


def sync_user_embeddings(user_id: int, db: Session) -> int:
    """
    同步用户向量数据（增强版）

    增强检查：
    1. 检查 FAISS 索引是否存在/完整
    2. 检查是否需要从数据库重建
    3. 检查数据库新增但未添加到 FAISS 的记录

    Returns: 补全的向量数量
    """
    manager = get_index_manager()

    # 1. 检查 FAISS 索引是否需要重建
    if manager.needs_rebuild(db, user_id):
        logger.info(f"[Startup Sync] 用户 {user_id}: FAISS 索引不完整或与数据库不一致，开始重建...")
        manager.rebuild_from_db(db, user_id)
        # 重建后返回 0，因为 rebuild_from_db 已经包含了完整重建
        return 0

    # 2. 找出没有向量的记录（仅补全缺失部分）
    entries_without_vector = db.query(Entry).filter(
        Entry.user_id == user_id,
        ~Entry.id.in_(
            db.query(Embedding.entry_id).filter(
                Embedding.entry_id == Entry.id,
                Embedding.entry_type == "main"
            )
        )
    ).all()

    if not entries_without_vector:
        logger.debug(f"[Startup Sync] 用户 {user_id}: 无需补全向量")
        return 0

    logger.info(f"[Startup Sync] 用户 {user_id}: 发现 {len(entries_without_vector)} 条记录缺少向量，开始补全...")

    count = 0
    for entry in entries_without_vector:
        try:
            content = entry.content
            title = content.split("\n")[0] if content else ""
            body = content.replace(title, "").strip() if title else content

            vectors = generate_title_content_embedding(title, body)

            embedding = Embedding(
                entry_id=entry.id,
                entry_type="main",
                title_vector=vectors["title_vector"],
                content_vector=vectors["content_vector"],
            )
            db.add(embedding)
            db.commit()
            count += 1

        except Exception as e:
            logger.error(f"[Startup Sync] 用户 {user_id} 记录 {entry.id} 向量生成失败: {e}")
            db.rollback()

    # 同步到 FAISS 索引
    if count > 0:
        try:
            manager = get_index_manager()
            # 重新构建该用户的索引（从数据库加载完整数据）
            _rebuild_user_index(user_id, db, manager)
            logger.info(f"[Startup Sync] 用户 {user_id}: 补全了 {count} 条向量，已同步到 FAISS")
        except Exception as e:
            logger.error(f"[Startup Sync] 用户 {user_id} FAISS 索引同步失败: {e}")

    return count


def _rebuild_user_index(user_id: int, db: Session, manager):
    """
    重建单个用户的 FAISS 索引
    """
    import ast
    # 获取该用户所有记录及其向量
    rows = db.query(Entry, Embedding).join(
        Embedding, Entry.id == Embedding.entry_id
    ).filter(
        Entry.user_id == user_id,
        Embedding.entry_type == "main"
    ).all()

    # 清除旧索引
    manager.cache.remove(user_id)

    # 重新添加所有向量
    for entry, embedding in rows:
        title = entry.content.split("\n")[0] if entry.content else ""
        body = entry.content.replace(title, "").strip() if title else entry.content

        # 解析向量（可能是 JSON 字符串）
        title_vec = embedding.title_vector
        content_vec = embedding.content_vector
        if isinstance(title_vec, str):
            title_vec = ast.literal_eval(title_vec)
        if isinstance(content_vec, str):
            content_vec = ast.literal_eval(content_vec)

        manager.add_vector(
            user_id,
            entry.id,
            title_vec,
            content_vec
        )

    logger.debug(f"[Startup Sync] 用户 {user_id}: 索引重建完成，共 {len(rows)} 条")
