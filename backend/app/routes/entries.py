"""
记录路由
POST /api/entries - 创建记录
GET /api/entries - 查询记录列表（支持分页和标签筛选，只返回顶级记录）
GET /api/entries/{id} - 获取单条记录详情（包含子记录）
PATCH /api/entries/{id} - 更新记录
DELETE /api/entries/{id} - 删除记录（含级联子记录/附件）
"""
import logging
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import or_
from typing import Optional
from app.database import get_db, SessionLocal
from app.models import Entry, EntryAttachment, EntryLabel, Embedding
from app.schemas import (
    EntryCreateRequest,
    EntryUpdateRequest,
    EntryResponse,
    EntryWithChildrenResponse,
    EntryListResponse
)
from app.auth import get_current_user, User
from app.services.embedding import generate_title_content_embedding


def _attachments_by_entry(db: Session, entry_ids: list[int]) -> dict[int, list[EntryAttachment]]:
    if not entry_ids:
        return {}
    rows = (
        db.query(EntryAttachment)
        .filter(EntryAttachment.entry_id.in_(entry_ids))
        .order_by(EntryAttachment.created_at.asc())
        .all()
    )
    attachment_map: dict[int, list[EntryAttachment]] = {}
    for attachment in rows:
        attachment_map.setdefault(attachment.entry_id, []).append(attachment)
    return attachment_map

logger = logging.getLogger(__name__)


def _generate_embedding_background(entry_id: int):
    """后台任务：为指定记录生成 embedding 向量，并更新 FAISS 索引"""
    from app.services.vector_search import add_entry_to_index, remove_entry_from_index

    db = SessionLocal()
    try:
        entry = db.query(Entry).filter(Entry.id == entry_id).first()
        if not entry:
            return

        content = entry.content or ""
        title = content.split("\n")[0] if content else ""
        body = content.replace(title, "").strip() if title else content

        vectors = generate_title_content_embedding(title, body)

        existing = db.query(Embedding).filter(
            Embedding.entry_id == entry.id
        ).first()

        if existing:
            existing.title_vector = vectors["title_vector"]
            existing.content_vector = vectors["content_vector"]
        else:
            embedding = Embedding(
                entry_id=entry.id,
                entry_type="main",
                title_vector=vectors["title_vector"],
                content_vector=vectors["content_vector"],
            )
            db.add(embedding)

        db.commit()

        # 更新 FAISS 索引（使用组合向量）
        combined_vec = [
            v * 0.3 + c * 0.7
            for v, c in zip(vectors["title_vector"], vectors["content_vector"])
        ]
        add_entry_to_index(entry.user_id, entry.id, vectors["title_vector"], vectors["content_vector"])

        logger.info(f"记录 {entry.id} 向量生成成功并已更新索引")
    except Exception as e:
        db.rollback()
        logger.error(f"记录 {entry_id} 向量生成失败: {e}")
    finally:
        db.close()

router = APIRouter()


@router.post("", response_model=EntryResponse, status_code=status.HTTP_201_CREATED)
def create_entry(
    request: EntryCreateRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    创建新记录
    - parent_id 为 NULL 时创建顶级记录
    - parent_id 不为空时创建子记录
    - embedding 向量在后台异步生成，不阻塞响应
    """
    label = db.query(EntryLabel).filter(
        EntryLabel.code == request.label_code,
        EntryLabel.is_active == True
    ).first()

    if label is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"标签不存在或未激活: {request.label_code}"
        )

    if request.parent_id is not None:
        parent_entry = db.query(Entry).filter(
            Entry.id == request.parent_id,
            Entry.user_id == current_user.id
        ).first()
        if parent_entry is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"父记录不存在: {request.parent_id}"
            )
        if parent_entry.label_code != request.label_code:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="父记录与当前标签不一致"
            )

    entry = Entry(
        user_id=current_user.id,
        label_code=request.label_code,
        content=request.content,
        parent_id=request.parent_id
    )

    db.add(entry)
    db.commit()
    db.refresh(entry)

    background_tasks.add_task(_generate_embedding_background, entry.id)

    return EntryResponse(
        id=entry.id,
        user_id=entry.user_id,
        label_code=entry.label_code,
        label_name=label.name,
        content=entry.content,
        parent_id=entry.parent_id,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
        attachments=[]
    )


@router.get("", response_model=EntryListResponse)
def get_entries(
    limit: int = Query(default=20, ge=1, le=100, description="每页数量"),
    offset: int = Query(default=0, ge=0, description="偏移量"),
    label_code: Optional[str] = Query(default=None, description="标签代码筛选"),
    q: Optional[str] = Query(
        default=None,
        max_length=200,
        description="可选服务端搜索：标题/正文/标签（仅当前用户）",
    ),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    查询记录列表
    - 只返回顶级记录（parent_id = NULL）
    - 每个顶级记录包含其子记录（children）
    - 按 created_at 倒序排列
    - 可选 q：在当前 user 隔离后搜索正文与标签名/code（标题为首行时由正文覆盖）
    """
    # 构建查询：只查询当前用户的顶级记录
    query = db.query(Entry).filter(
        Entry.user_id == current_user.id,
        Entry.parent_id == None
    )

    # 标签筛选
    if label_code:
        # 验证标签是否存在且激活
        label = db.query(EntryLabel).filter(
            EntryLabel.code == label_code,
            EntryLabel.is_active == True
        ).first()
        if label is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"标签不存在或未激活: {label_code}"
            )
        query = query.filter(Entry.label_code == label_code)

    search = (q or "").strip()
    if search:
        term = f"%{search}%"
        query = (
            query.outerjoin(EntryLabel, Entry.label_code == EntryLabel.code)
            .filter(
                or_(
                    Entry.content.like(term),
                    Entry.label_code.like(term),
                    EntryLabel.name.like(term),
                )
            )
            .distinct()
        )

    # 获取总数（顶级记录）
    total = query.count()

    # 排序和分页
    entries = query.order_by(Entry.created_at.desc()).offset(offset).limit(limit).all()

    # 批量加载子记录
    entry_ids = [entry.id for entry in entries]
    if entry_ids:
        children_map = {}
        children_query = db.query(Entry).filter(
            Entry.parent_id.in_(entry_ids)
        ).order_by(Entry.created_at.asc()).all()

        for child in children_query:
            if child.parent_id not in children_map:
                children_map[child.parent_id] = []
            children_map[child.parent_id].append(child)

        # 获取所有需要的标签名称和附件
        all_label_codes = {entry.label_code for entry in entries}
        all_entry_ids = set(entry_ids)
        for children in children_map.values():
            for child in children:
                all_label_codes.add(child.label_code)
                all_entry_ids.add(child.id)

        labels = db.query(EntryLabel).filter(EntryLabel.code.in_(all_label_codes)).all()
        label_map = {label.code: label.name for label in labels}
        attachment_map = _attachments_by_entry(db, list(all_entry_ids))

        # 构建响应（包含 children）
        items = []
        for entry in entries:
            children = children_map.get(entry.id, [])
            item = EntryWithChildrenResponse(
                id=entry.id,
                user_id=entry.user_id,
                label_code=entry.label_code,
                label_name=label_map.get(entry.label_code, entry.label_code),
                content=entry.content,
                parent_id=entry.parent_id,
                created_at=entry.created_at,
                updated_at=entry.updated_at,
                attachments=attachment_map.get(entry.id, []),
                children=[
                    EntryWithChildrenResponse(
                        id=child.id,
                        user_id=child.user_id,
                        label_code=child.label_code,
                        label_name=label_map.get(child.label_code, child.label_code),
                        content=child.content,
                        parent_id=child.parent_id,
                        created_at=child.created_at,
                        updated_at=child.updated_at,
                        attachments=attachment_map.get(child.id, []),
                        children=[]
                    )
                    for child in children
                ]
            )
            items.append(item)
    else:
        items = []

    return EntryListResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset
    )


@router.get("/{entry_id}", response_model=EntryWithChildrenResponse)
def get_entry(
    entry_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    获取单条记录详情（包含子记录）
    """
    entry = db.query(Entry).filter(
        Entry.id == entry_id,
        Entry.user_id == current_user.id
    ).first()

    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"记录不存在: {entry_id}"
        )

    # 获取标签名称
    label = db.query(EntryLabel).filter(
        EntryLabel.code == entry.label_code
    ).first()
    label_name = label.name if label else entry.label_code

    # 获取子记录
    children = db.query(Entry).filter(
        Entry.parent_id == entry_id
    ).order_by(Entry.created_at.asc()).all()

    # 获取子记录的标签名称
    children_label_codes = {child.label_code for child in children}
    children_labels = db.query(EntryLabel).filter(
        EntryLabel.code.in_(children_label_codes)
    ).all() if children_label_codes else []
    children_label_map = {label.code: label.name for label in children_labels}
    attachment_map = _attachments_by_entry(db, [entry.id, *[child.id for child in children]])

    return EntryWithChildrenResponse(
        id=entry.id,
        user_id=entry.user_id,
        label_code=entry.label_code,
        label_name=label_name,
        content=entry.content,
        parent_id=entry.parent_id,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
        attachments=attachment_map.get(entry.id, []),
        children=[
            EntryWithChildrenResponse(
                id=child.id,
                user_id=child.user_id,
                label_code=child.label_code,
                label_name=children_label_map.get(child.label_code, child.label_code),
                content=child.content,
                parent_id=child.parent_id,
                created_at=child.created_at,
                updated_at=child.updated_at,
                attachments=attachment_map.get(child.id, []),
                children=[]
            )
            for child in children
        ]
    )


def _require_own_entry_mutation(current_user: User) -> None:
    """Own-entry PATCH/DELETE requires explicit can_edit_delete_own_entries (not is_admin)."""
    if bool(getattr(current_user, "can_edit_delete_own_entries", False)):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "code": "ENTRY_MUTATION_FORBIDDEN",
            "message": "未获得修改或删除原记录的权限",
        },
    )


@router.patch("/{entry_id}", response_model=EntryResponse)
def update_entry(
    entry_id: int,
    request: EntryUpdateRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    更新记录内容，embedding 在后台异步重新生成。
    需要 can_edit_delete_own_entries；跨用户/不存在仍 404。
    """
    entry = db.query(Entry).filter(
        Entry.id == entry_id,
        Entry.user_id == current_user.id
    ).first()

    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="记录不存在",
        )

    _require_own_entry_mutation(current_user)

    if request.content is not None:
        entry.content = request.content

    db.commit()
    db.refresh(entry)

    if request.content is not None:
        background_tasks.add_task(_generate_embedding_background, entry.id)

    label = db.query(EntryLabel).filter(
        EntryLabel.code == entry.label_code
    ).first()
    label_name = label.name if label else entry.label_code

    return EntryResponse(
        id=entry.id,
        user_id=entry.user_id,
        label_code=entry.label_code,
        label_name=label_name,
        content=entry.content,
        parent_id=entry.parent_id,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
        attachments=_attachments_by_entry(db, [entry.id]).get(entry.id, [])
    )


@router.delete("/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_entry(
    entry_id: int,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """删除当前用户的记录树（子记录/附件/knowledge/向量/文件）。"""
    from app.services.entry_deletion import (
        EntryDeletionError,
        delete_owned_entry_tree,
        finalize_entry_deletion_side_effects,
    )

    owned = db.query(Entry.id).filter(
        Entry.id == entry_id,
        Entry.user_id == current_user.id,
    ).first()
    if owned is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="记录不存在",
        )

    _require_own_entry_mutation(current_user)

    try:
        result = delete_owned_entry_tree(
            db,
            user_id=int(current_user.id),
            entry_id=int(entry_id),
        )
        db.commit()
    except EntryDeletionError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="记录不存在",
        )
    except Exception:
        db.rollback()
        logging.getLogger(__name__).warning(
            "entry_delete_failed user_id=%s entry_id=%s",
            int(current_user.id),
            int(entry_id),
        )
        raise

    background_tasks.add_task(finalize_entry_deletion_side_effects, result)
    return None
