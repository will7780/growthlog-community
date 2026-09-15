"""
Parent/root context for retrieval hits on child entries or child attachments.

Option B: do not change upload attachment target; enrich hits at retrieve/cite time
with parent_id / root_entry_id / parent_snippet so UI and LLM see parent context.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from sqlalchemy.orm import Session

from app.models import Entry, EntryAttachment
from app.services.attachment_sort_order import attachment_sort_key

PARENT_SNIPPET_MAX = 260
MAX_FAMILY_DEPTH = 32

# R11.7-Fix2 hard budgets for family expand → answer context.
DEFAULT_MAX_FAMILIES = 24
EXPAND_MAX_REFS = 64
EXPAND_MAX_CHARS = 48000
MAX_DOC_CHUNKS_PER_ATTACHMENT = 4
ANSWER_CONTEXT_MAX_REFS = 20
ANSWER_CONTEXT_MAX_CHARS = 16000


@dataclass
class RecordFamilyAttachment:
    attachment_id: int
    entry_id: int
    sort_order: int
    path: str
    content_hash: str
    status: str


@dataclass
class RecordFamilyEntry:
    entry_id: int
    parent_id: Optional[int]
    order: int
    path: str
    attachments: List[RecordFamilyAttachment] = field(default_factory=list)


@dataclass
class RecordFamily:
    root_entry_id: int
    hit_entry_id: Optional[int]
    hit_attachment_id: Optional[int]
    entries: List[RecordFamilyEntry] = field(default_factory=list)

    def ordered_attachment_ids(self) -> List[int]:
        ids: List[int] = []
        for entry in self.entries:
            for att in entry.attachments:
                ids.append(att.attachment_id)
        return ids


def _snippet_from_content(content: str, max_len: int = PARENT_SNIPPET_MAX) -> tuple[str, str]:
    text = (content or "").strip()
    if not text:
        return "", ""
    title = text.split("\n", 1)[0].strip()
    body = text[len(title) :].lstrip("\n").strip() if title else text
    snippet = (body or title)[:max_len]
    return title[:120], snippet


def load_parent_context_map(
    db: Session,
    user_id: int,
    entry_ids: Iterable[Optional[int]],
) -> Dict[int, Dict[str, Any]]:
    """
    Batch-load entry → parent/root fields for the given entry ids.
    Keys are entry_id; values always include parent_id and root_entry_id.
    """
    ids: List[int] = []
    for raw in entry_ids:
        if raw is None:
            continue
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    unique = sorted(set(ids))
    if not unique:
        return {}

    entries = (
        db.query(Entry)
        .filter(Entry.user_id == user_id, Entry.id.in_(unique))
        .all()
    )
    by_id = {int(e.id): e for e in entries}

    parent_ids = {int(e.parent_id) for e in entries if e.parent_id is not None}
    parents: Dict[int, Entry] = {}
    if parent_ids:
        parents = {
            int(p.id): p
            for p in db.query(Entry)
            .filter(Entry.user_id == user_id, Entry.id.in_(sorted(parent_ids)))
            .all()
        }

    out: Dict[int, Dict[str, Any]] = {}
    for eid, entry in by_id.items():
        parent_id = int(entry.parent_id) if entry.parent_id is not None else None
        root_id = parent_id if parent_id is not None else eid
        parent = parents.get(parent_id) if parent_id is not None else None
        # One-level family today; if parent itself is nested, prefer its parent as root.
        if parent is not None and parent.parent_id is not None:
            root_id = int(parent.parent_id)

        payload: Dict[str, Any] = {
            "parent_id": parent_id,
            "root_entry_id": int(root_id),
            "parent_title": None,
            "parent_snippet": None,
        }
        if parent is not None:
            title, snippet = _snippet_from_content(parent.content or "")
            payload["parent_title"] = title or f"记录 #{parent.id}"
            payload["parent_snippet"] = snippet
        out[eid] = payload
    return out


def apply_parent_context(
    ref: Mapping[str, Any],
    context_by_entry_id: Mapping[int, Mapping[str, Any]],
    *,
    inject_parent_into_content: bool = True,
) -> Dict[str, Any]:
    """Return a shallow copy of ref with parent/root fields filled when available."""
    item = dict(ref)
    raw_eid = item.get("entry_id")
    try:
        eid = int(raw_eid) if raw_eid is not None else None
    except (TypeError, ValueError):
        eid = None
    if eid is None:
        return item

    ctx = context_by_entry_id.get(eid)
    if not ctx:
        item.setdefault("parent_id", None)
        item.setdefault("root_entry_id", eid)
        return item

    parent_id = ctx.get("parent_id")
    root_id = int(ctx.get("root_entry_id") or eid)
    item["parent_id"] = parent_id
    item["root_entry_id"] = root_id
    parent_title = ctx.get("parent_title")
    parent_snippet = ctx.get("parent_snippet")
    if parent_title:
        item["parent_title"] = parent_title
    if parent_snippet:
        item["parent_snippet"] = parent_snippet

    meta = dict(item.get("metadata") or {})
    if parent_id is not None:
        meta["parent_id"] = parent_id
    meta["root_entry_id"] = root_id
    item["metadata"] = meta

    if inject_parent_into_content and parent_snippet:
        child_content = str(item.get("content") or "")
        prefix = f"[父记录] {parent_snippet}"
        if prefix not in child_content:
            item["content"] = f"{prefix}\n\n{child_content}".strip()
        snippet = str(item.get("snippet") or "")
        if snippet and not snippet.startswith("[父记录]"):
            head = parent_snippet[:80]
            item["snippet"] = f"{head}… | {snippet}" if len(parent_snippet) > 80 else f"{head} | {snippet}"

    return item


def enrich_references_with_parent_context(
    db: Session,
    user_id: int,
    refs: Sequence[Mapping[str, Any]],
    *,
    inject_parent_into_content: bool = True,
) -> List[Dict[str, Any]]:
    """Enrich a list of retrieval hits with parent/root context (batched)."""
    if not refs:
        return []
    context_map = load_parent_context_map(
        db,
        user_id,
        (ref.get("entry_id") for ref in refs),
    )
    return [
        apply_parent_context(
            ref,
            context_map,
            inject_parent_into_content=inject_parent_into_content,
        )
        for ref in refs
    ]


def enrich_reference_lists(
    db: Session,
    user_id: int,
    *lists: Sequence[Mapping[str, Any]],
    inject_parent_into_content: bool = True,
) -> List[List[Dict[str, Any]]]:
    """Enrich multiple hit lists with one parent lookup batch."""
    entry_ids: List[Optional[int]] = []
    for lst in lists:
        for ref in lst or []:
            entry_ids.append(ref.get("entry_id") if isinstance(ref, Mapping) else None)
    context_map = load_parent_context_map(db, user_id, entry_ids)
    enriched: List[List[Dict[str, Any]]] = []
    for lst in lists:
        enriched.append(
            [
                apply_parent_context(
                    ref if isinstance(ref, Mapping) else {},
                    context_map,
                    inject_parent_into_content=inject_parent_into_content,
                )
                for ref in (lst or [])
            ]
        )
    return enriched


def resolve_root_entry_id(db: Session, user_id: int, entry_id: int) -> Optional[int]:
    """Walk parent chain to root with cycle and depth protection."""
    current_id = int(entry_id)
    seen: Set[int] = set()
    depth = 0
    while depth < MAX_FAMILY_DEPTH:
        if current_id in seen:
            return None
        seen.add(current_id)
        entry = (
            db.query(Entry)
            .filter(Entry.id == current_id, Entry.user_id == user_id)
            .first()
        )
        if entry is None:
            return None
        if entry.parent_id is None:
            return int(entry.id)
        current_id = int(entry.parent_id)
        depth += 1
    return None


def resolve_family_from_hit(
    db: Session,
    user_id: int,
    *,
    entry_id: Optional[int] = None,
    attachment_id: Optional[int] = None,
) -> Optional[RecordFamily]:
    hit_entry_id: Optional[int] = entry_id
    hit_attachment_id: Optional[int] = attachment_id

    if attachment_id is not None:
        attachment = (
            db.query(EntryAttachment)
            .filter(
                EntryAttachment.id == attachment_id,
                EntryAttachment.user_id == user_id,
            )
            .first()
        )
        if attachment is None or attachment.status == "failed":
            return None
        hit_entry_id = int(attachment.entry_id)
        hit_attachment_id = int(attachment.id)

    if hit_entry_id is None:
        return None

    root_id = resolve_root_entry_id(db, user_id, hit_entry_id)
    if root_id is None:
        return None

    return build_record_family(
        db,
        user_id,
        root_id,
        hit_entry_id=hit_entry_id,
        hit_attachment_id=hit_attachment_id,
    )


def build_record_family(
    db: Session,
    user_id: int,
    root_entry_id: int,
    *,
    hit_entry_id: Optional[int] = None,
    hit_attachment_id: Optional[int] = None,
) -> Optional[RecordFamily]:
    root = (
        db.query(Entry)
        .filter(Entry.id == root_entry_id, Entry.user_id == user_id)
        .first()
    )
    if root is None:
        return None

    all_entries = (
        db.query(Entry)
        .filter(Entry.user_id == user_id)
        .all()
    )
    by_id = {int(e.id): e for e in all_entries}
    if root_entry_id not in by_id:
        return None

    children_map: Dict[Optional[int], List[Entry]] = {}
    for entry in all_entries:
        pid = int(entry.parent_id) if entry.parent_id is not None else None
        children_map.setdefault(pid, []).append(entry)
    for pid in children_map:
        children_map[pid].sort(key=lambda e: (e.created_at, int(e.id)))

    ordered_entries: List[Tuple[int, str]] = []

    def walk(entry_id: int, path: str, depth: int) -> None:
        if depth > MAX_FAMILY_DEPTH:
            return
        if entry_id not in by_id:
            return
        ordered_entries.append((entry_id, path))
        for child in children_map.get(entry_id, []):
            walk(int(child.id), f"{path}/{child.id}", depth + 1)

    walk(root_entry_id, str(root_entry_id), 0)

    entry_ids = [eid for eid, _ in ordered_entries]
    attachments = (
        db.query(EntryAttachment)
        .filter(
            EntryAttachment.user_id == user_id,
            EntryAttachment.entry_id.in_(entry_ids),
        )
        .all()
    )
    attachments_by_entry: Dict[int, List[EntryAttachment]] = {}
    for att in attachments:
        if att.status == "failed":
            continue
        attachments_by_entry.setdefault(int(att.entry_id), []).append(att)
    for eid in attachments_by_entry:
        attachments_by_entry[eid].sort(key=attachment_sort_key)

    family_entries: List[RecordFamilyEntry] = []
    for order_idx, (eid, path) in enumerate(ordered_entries):
        att_rows = attachments_by_entry.get(eid, [])
        seen_attachment_ids: Set[int] = set()
        att_dtos: List[RecordFamilyAttachment] = []
        for att in att_rows:
            aid = int(att.id)
            if aid in seen_attachment_ids:
                continue
            seen_attachment_ids.add(aid)
            att_dtos.append(
                RecordFamilyAttachment(
                    attachment_id=aid,
                    entry_id=int(att.entry_id),
                    sort_order=int(att.sort_order if att.sort_order is not None else 0),
                    path=f"{path}/a{aid}",
                    content_hash=str(att.content_hash or ""),
                    status=str(att.status or ""),
                )
            )
        entry = by_id[eid]
        family_entries.append(
            RecordFamilyEntry(
                entry_id=eid,
                parent_id=int(entry.parent_id) if entry.parent_id is not None else None,
                order=order_idx,
                path=path,
                attachments=att_dtos,
            )
        )

    return RecordFamily(
        root_entry_id=root_entry_id,
        hit_entry_id=hit_entry_id,
        hit_attachment_id=hit_attachment_id,
        entries=family_entries,
    )


def _safe_entry_title(content: str) -> str:
    title, _ = _snippet_from_content(content)
    return (title or "记录")[:120]


def _json_safe_created_at(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    return value


def _safe_attachment_filename(name: Optional[str]) -> str:
    raw = (name or "").strip()
    if not raw:
        return "附件"
    # Basename-only; never expose paths or DB ids.
    base = raw.replace("\\", "/").rsplit("/", 1)[-1].strip()
    cleaned = "".join(ch for ch in base if ch.isprintable() and ch not in {"/", "\\"})
    return (cleaned or "附件")[:120]


def _chunk_meta(chunk: Any) -> Dict[str, Any]:
    meta = getattr(chunk, "metadata_json", None)
    return meta if isinstance(meta, dict) else {}


def _chunk_eligible_for_expand(chunk: Any) -> bool:
    """OCR must pass search-eligibility; other modalities need non-empty text."""
    from app.services.ocr_quality import is_ocr_chunk_search_eligible

    modality = str(getattr(chunk, "modality", "") or "")
    content = str(getattr(chunk, "content", "") or "").strip()
    if modality == "ocr":
        return is_ocr_chunk_search_eligible(_chunk_meta(chunk))
    return bool(content)


def _is_image_attachment(att: Any) -> bool:
    mime = str(getattr(att, "mime_type", "") or "").lower()
    ext = str(getattr(att, "file_ext", "") or "").lower().lstrip(".")
    if mime.startswith("image/"):
        return True
    return ext in {"png", "jpg", "jpeg", "webp", "gif", "bmp", "tif", "tiff", "heic"}


def _is_long_document(att: Any) -> bool:
    mime = str(getattr(att, "mime_type", "") or "").lower()
    ext = str(getattr(att, "file_ext", "") or "").lower().lstrip(".")
    if "pdf" in mime or ext == "pdf":
        return True
    if "presentation" in mime or ext in {"pptx", "ppt"}:
        return True
    return False


def _select_chunks_for_attachment(
    att: Any,
    att_chunks: Sequence[Any],
    *,
    hit_att_i: Optional[int],
    hit_chunk_id: Any,
    hit_page: Any,
    hit_slide: Any,
    max_doc_chunks: int,
) -> List[Any]:
    """
    Image: all eligible OCR by chunk_index; caption only if no eligible OCR.
    PDF/PPTX: prefer precise hit page/slide, then fill by chunk_index up to budget.
    Other: eligible chunks by chunk_index within budget.
    """
    eligible = [ch for ch in att_chunks if _chunk_eligible_for_expand(ch)]
    if not eligible:
        # Caption fallback when OCR exists but none eligible, or no OCR at all.
        captions = [
            ch
            for ch in att_chunks
            if str(getattr(ch, "modality", "") or "") == "caption"
            and str(getattr(ch, "content", "") or "").strip()
        ]
        if captions and _is_image_attachment(att):
            return sorted(captions, key=lambda c: int(getattr(c, "chunk_index", 0) or 0))[:1]
        return []

    if _is_image_attachment(att):
        ocr_ok = [
            ch
            for ch in eligible
            if str(getattr(ch, "modality", "") or "") == "ocr"
        ]
        if ocr_ok:
            return sorted(ocr_ok, key=lambda c: int(getattr(c, "chunk_index", 0) or 0))
        # Non-OCR eligible on image (rare): treat like caption path.
        return sorted(eligible, key=lambda c: int(getattr(c, "chunk_index", 0) or 0))[:1]

    # Prefer precise hit identity first for documents.
    preferred: List[Any] = []
    for ch in eligible:
        if hit_chunk_id is not None and int(getattr(ch, "id", -1) or -1) == int(hit_chunk_id):
            preferred = [ch]
            break
        if (
            hit_att_i is not None
            and int(att.id) == hit_att_i
            and hit_page is not None
            and getattr(ch, "page_no", None) is not None
            and int(ch.page_no) == int(hit_page)
        ):
            preferred = [ch]
            break
        if (
            hit_att_i is not None
            and int(att.id) == hit_att_i
            and hit_slide is not None
            and getattr(ch, "slide_no", None) is not None
            and int(ch.slide_no) == int(hit_slide)
        ):
            preferred = [ch]
            break

    ordered = sorted(eligible, key=lambda c: int(getattr(c, "chunk_index", 0) or 0))
    if not preferred:
        return ordered[: max(1, int(max_doc_chunks))]

    selected: List[Any] = list(preferred)
    seen_ids = {int(getattr(preferred[0], "id", -1) or -1)}
    for ch in ordered:
        cid = int(getattr(ch, "id", -1) or -1)
        if cid in seen_ids:
            continue
        if len(selected) >= max(1, int(max_doc_chunks)):
            break
        selected.append(ch)
        seen_ids.add(cid)
    return selected


def expand_ranked_hits_to_family_context(
    db: Session,
    user_id: int,
    ranked_hits: Sequence[Mapping[str, Any]],
    *,
    max_families: int = DEFAULT_MAX_FAMILIES,
    max_refs: int = EXPAND_MAX_REFS,
    max_chars: int = EXPAND_MAX_CHARS,
    max_doc_chunks_per_attachment: int = MAX_DOC_CHUNKS_PER_ATTACHMENT,
) -> List[Dict[str, Any]]:
    """
    R11.7-Fix2: expand high-score hits into full record families with hard budgets.

    Order: root → children preorder; attachments by sort_order, created_at, id.
    Only indexed attachments; OCR via is_ocr_chunk_search_eligible; caption only
    when no eligible OCR. PDF/PPTX keep page/slide identity within doc chunk budget.
    Model-facing titles never include database IDs.
    """
    from app.models import AttachmentChunk, Entry, EntryAttachment
    from app.services.reference_identity import (
        annotate_with_canonical_keys,
        evidence_origin_key,
    )

    if not ranked_hits:
        return []

    out: List[Dict[str, Any]] = []
    seen_origins: Set[str] = set()
    families_done: Set[int] = set()
    total_chars = 0

    def _can_accept(text: str) -> bool:
        if len(out) >= int(max_refs):
            return False
        if total_chars + len(text or "") > int(max_chars):
            return False
        return True

    def _push(ref: Dict[str, Any]) -> bool:
        nonlocal total_chars
        annotated = annotate_with_canonical_keys([ref])[0]
        origin = evidence_origin_key(annotated)
        if origin in seen_origins:
            return False
        body = str(annotated.get("content") or annotated.get("snippet") or "")
        if not _can_accept(body):
            return False
        seen_origins.add(origin)
        out.append(annotated)
        total_chars += len(body)
        return True

    for hit in ranked_hits:
        if len(out) >= int(max_refs) or total_chars >= int(max_chars):
            break

        hit_entry_id = hit.get("entry_id")
        hit_attachment_id = hit.get("attachment_id")
        try:
            hit_entry_id_i = int(hit_entry_id) if hit_entry_id is not None else None
        except (TypeError, ValueError):
            hit_entry_id_i = None
        try:
            hit_att_i = int(hit_attachment_id) if hit_attachment_id is not None else None
        except (TypeError, ValueError):
            hit_att_i = None

        family = resolve_family_from_hit(
            db,
            user_id,
            entry_id=hit_entry_id_i,
            attachment_id=hit_att_i,
        )
        if family is None:
            _push(dict(hit))
            continue

        root_id = int(family.root_entry_id)
        if root_id in families_done:
            _push(dict(hit))
            continue

        # Hard family cap: never expand more than max_families families.
        if len(families_done) >= int(max_families):
            _push(dict(hit))
            continue

        families_done.add(root_id)

        entry_ids = [fe.entry_id for fe in family.entries]
        entries = (
            db.query(Entry)
            .filter(Entry.user_id == user_id, Entry.id.in_(entry_ids))
            .all()
        )
        entry_by_id = {int(e.id): e for e in entries}
        attachments = (
            db.query(EntryAttachment)
            .filter(
                EntryAttachment.user_id == user_id,
                EntryAttachment.entry_id.in_(entry_ids),
            )
            .all()
        )
        # Only indexed attachments enter answer context.
        att_by_id = {
            int(a.id): a
            for a in attachments
            if str(getattr(a, "status", "") or "") == "indexed"
        }
        chunks = (
            db.query(AttachmentChunk)
            .filter(
                AttachmentChunk.user_id == user_id,
                AttachmentChunk.attachment_id.in_(list(att_by_id.keys()) or [-1]),
            )
            .order_by(AttachmentChunk.attachment_id, AttachmentChunk.chunk_index)
            .all()
            if att_by_id
            else []
        )
        chunks_by_att: Dict[int, List[Any]] = {}
        for ch in chunks:
            chunks_by_att.setdefault(int(ch.attachment_id), []).append(ch)

        hit_page = hit.get("page_no")
        hit_slide = hit.get("slide_no")
        hit_chunk_id = hit.get("chunk_id")

        for fe in family.entries:
            if len(out) >= int(max_refs) or total_chars >= int(max_chars):
                break
            entry = entry_by_id.get(int(fe.entry_id))
            if entry is None:
                continue
            content = str(entry.content or "")
            title = _safe_entry_title(content)
            _, snippet = _snippet_from_content(content)
            _push(
                {
                    "source_type": "entry",
                    "entry_id": int(entry.id),
                    "source_id": int(entry.id),
                    "reference_key": f"entry:source:{int(entry.id)}",
                    "title": title,
                    "content": content,
                    "snippet": snippet or content[:260],
                    "created_at": _json_safe_created_at(entry.created_at),
                    "parent_id": int(entry.parent_id) if entry.parent_id is not None else None,
                    "root_entry_id": root_id,
                    "retrieval_method": "family_expand",
                    "relevance_level": hit.get("relevance_level"),
                }
            )
            for att_dto in fe.attachments:
                if len(out) >= int(max_refs) or total_chars >= int(max_chars):
                    break
                att = att_by_id.get(int(att_dto.attachment_id))
                if att is None:
                    continue
                att_chunks = chunks_by_att.get(int(att.id), [])
                chosen_list = _select_chunks_for_attachment(
                    att,
                    att_chunks,
                    hit_att_i=hit_att_i,
                    hit_chunk_id=hit_chunk_id,
                    hit_page=hit_page,
                    hit_slide=hit_slide,
                    max_doc_chunks=max_doc_chunks_per_attachment
                    if _is_long_document(att)
                    else max_doc_chunks_per_attachment,
                )
                filename = _safe_attachment_filename(
                    str(getattr(att, "original_filename", None) or "")
                )
                for chosen in chosen_list:
                    if len(out) >= int(max_refs) or total_chars >= int(max_chars):
                        break
                    page_no = getattr(chosen, "page_no", None)
                    slide_no = getattr(chosen, "slide_no", None)
                    modality = str(getattr(chosen, "modality", None) or "attachment")
                    label = filename
                    if page_no is not None:
                        label = f"{filename} · 第{int(page_no)}页"
                    elif slide_no is not None:
                        label = f"{filename} · 第{int(slide_no)}张"
                    elif modality == "ocr":
                        label = f"{filename} · 原图"
                    elif modality == "caption":
                        label = f"{filename} · 说明"
                    text = str(getattr(chosen, "content", "") or "")
                    chunk_index = int(getattr(chosen, "chunk_index", 0) or 0)
                    chunk_id = int(chosen.id)
                    _push(
                        {
                            "source_type": "attachment_chunk",
                            "entry_id": int(att.entry_id),
                            "source_id": chunk_id,
                            "attachment_id": int(att.id),
                            "chunk_id": chunk_id,
                            "chunk_index": chunk_index,
                            "reference_key": f"attachment_chunk:chunk:{chunk_id}",
                            "title": label,
                            "content": text,
                            "snippet": text[:420],
                            "modality": modality,
                            "page_no": int(page_no) if page_no is not None else None,
                            "slide_no": int(slide_no) if slide_no is not None else None,
                            "root_entry_id": root_id,
                            "retrieval_method": "family_expand",
                            "relevance_level": hit.get("relevance_level"),
                            "metadata": {"filename": filename},
                        }
                    )

        # Retain the original ranked hit identity (OCR/PDF/PPTX precision).
        _push(dict(hit))

    return out


def select_answer_context_refs(
    expanded_refs: Sequence[Mapping[str, Any]],
    ranked_hits: Sequence[Mapping[str, Any]] = (),
    *,
    max_refs: int = ANSWER_CONTEXT_MAX_REFS,
    max_chars: int = ANSWER_CONTEXT_MAX_CHARS,
) -> List[Dict[str, Any]]:
    """
    Hard-budget answer context shared by non-stream and SSE generation.

    Family order: families ranked by best hit score; within each family keep
    expanded preorder (root → children → attachments → chunks). Precise hits are
    budget-reserved but never reordered ahead of earlier family members.
    Oversized non-essential members are skipped (continue), not aborting the walk.
    """
    from app.services.reference_identity import (
        annotate_with_canonical_keys,
        evidence_origin_key,
    )

    expanded = annotate_with_canonical_keys([dict(r) for r in expanded_refs])
    if not expanded:
        return []

    by_origin: Dict[str, Dict[str, Any]] = {}
    order_origins: List[str] = []
    for ref in expanded:
        origin = evidence_origin_key(ref)
        if origin in by_origin:
            continue
        by_origin[origin] = ref
        order_origins.append(origin)

    hit_origins: Set[str] = set()
    hit_score: Dict[str, int] = {}
    for hit in ranked_hits:
        annotated = annotate_with_canonical_keys([dict(hit)])[0]
        origin = evidence_origin_key(annotated)
        if origin not in by_origin:
            continue
        hit_origins.add(origin)
        score = int(annotated.get("relevance_level") or hit.get("relevance_level") or 0)
        hit_score[origin] = max(hit_score.get(origin, 0), score)

    # Todo answers must retain the precise hit and its ancestor path before
    # spending the remaining reference budget on siblings from the same tree.
    required_origins: Set[str] = set(hit_origins)
    for hit_origin in list(hit_origins):
        hit_ref = by_origin.get(hit_origin) or {}
        if str(hit_ref.get("source_type") or "") != "todo":
            continue
        hit_meta = hit_ref.get("metadata") if isinstance(hit_ref.get("metadata"), dict) else {}
        hit_path = hit_meta.get("path_titles")
        if not isinstance(hit_path, list) or not hit_path:
            continue
        for candidate_origin, candidate_ref in by_origin.items():
            if str(candidate_ref.get("source_type") or "") != "todo":
                continue
            candidate_meta = (
                candidate_ref.get("metadata")
                if isinstance(candidate_ref.get("metadata"), dict)
                else {}
            )
            candidate_path = candidate_meta.get("path_titles")
            if (
                isinstance(candidate_path, list)
                and candidate_path
                and len(candidate_path) <= len(hit_path)
                and candidate_path == hit_path[: len(candidate_path)]
            ):
                required_origins.add(candidate_origin)

    # Group records by root_entry_id and Todos by root_todo_id while preserving
    # each family/tree's expansion order.
    family_order: List[Any] = []
    family_members: Dict[Any, List[str]] = {}
    for origin in order_origins:
        ref = by_origin[origin]
        if str(ref.get("source_type") or "") == "todo":
            root = ref.get("root_todo_id") or ref.get("todo_id") or ref.get("source_id")
            root = f"todo:{root}" if root is not None else None
        else:
            root = ref.get("root_entry_id")
            if root is None:
                root = ref.get("entry_id")
        if root is None:
            root = f"orphan:{origin}"
        if root not in family_members:
            family_members[root] = []
            family_order.append(root)
        family_members[root].append(origin)

    def _family_score(root: Any) -> Tuple[int, int]:
        members = family_members.get(root) or []
        best = 0
        first_idx = len(order_origins)
        for origin in members:
            best = max(best, int(hit_score.get(origin, 0)))
            if origin in order_origins:
                first_idx = min(first_idx, order_origins.index(origin))
        return (-best, first_idx)

    family_order.sort(key=_family_score)

    # Walk families in score order; within family keep preorder.
    walk: List[str] = []
    for root in family_order:
        walk.extend(family_members[root])

    selected: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    total_chars = 0

    def _body_of(origin: str) -> str:
        ref = by_origin[origin]
        return str(ref.get("content") or ref.get("snippet") or "")

    # Budget reservation for precise hits so a long root cannot starve them.
    RESERVE_PER_HIT = 800
    reserved_need = 0
    for origin in walk:
        if origin in required_origins:
            reserved_need += min(RESERVE_PER_HIT, len(_body_of(origin)))
    reserved_need = min(reserved_need, max(int(max_chars) // 2, RESERVE_PER_HIT))

    def _remaining_reserve_after(idx: int) -> int:
        need = 0
        for later in walk[idx + 1 :]:
            if later in required_origins and later not in seen:
                need += min(RESERVE_PER_HIT, len(_body_of(later)))
        return min(need, reserved_need)

    def _append(ref: Dict[str, Any], origin: str, body: str) -> None:
        nonlocal total_chars
        selected.append(ref)
        seen.add(origin)
        total_chars += len(body)

    for idx, origin in enumerate(walk):
        if origin in seen:
            continue
        if len(selected) >= int(max_refs):
            break
        remaining_required = sum(
            1
            for later in walk[idx:]
            if later in required_origins and later not in seen
        )
        if (
            origin not in required_origins
            and len(selected) + remaining_required >= int(max_refs)
        ):
            continue
        ref = by_origin[origin]
        body = _body_of(origin)
        is_reserved = origin in required_origins
        room_total = int(max_chars) - total_chars
        if room_total <= 0:
            continue
        if is_reserved:
            take = min(len(body), room_total)
            if take <= 0:
                continue
            if take < len(body):
                trimmed = dict(ref)
                trimmed["content"] = body[:take]
                trimmed["snippet"] = str(trimmed.get("snippet") or body)[: min(420, take)]
                _append(trimmed, origin, str(trimmed.get("content") or ""))
            else:
                _append(ref, origin, body)
            continue
        # Non-essential: leave room for later reserved hits.
        hold = _remaining_reserve_after(idx)
        room = room_total - hold
        if room <= 0:
            continue
        if len(body) <= room:
            _append(ref, origin, body)
            continue
        # Soft-truncate oversized neighbors so family order can be kept
        # without starving later precise hits.
        if room >= 200:
            trimmed = dict(ref)
            trimmed["content"] = body[:room]
            trimmed["snippet"] = str(trimmed.get("snippet") or body)[: min(420, room)]
            _append(trimmed, origin, str(trimmed.get("content") or ""))
            continue
        # Room too small to be useful: skip, do not abort the walk.
        continue

    return selected
