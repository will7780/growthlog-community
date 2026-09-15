"""
Shared attachment cleanup helpers (delete attachment route + entry tree delete).

Only operates on caller-provided, already-authorized attachment IDs.
"""
from __future__ import annotations

import logging
from typing import Iterable, Sequence

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.services.attachment_storage import delete_stored_file

logger = logging.getLogger(__name__)


def _unique_positive_ids(ids: Iterable[int]) -> list[int]:
    seen: set[int] = set()
    out: list[int] = []
    for raw in ids:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value <= 0 or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def cancel_attachment_jobs(db: Session, attachment_ids: Sequence[int]) -> int:
    """Cancel pending/processing jobs for exact attachment IDs."""
    aids = _unique_positive_ids(attachment_ids)
    if not aids:
        return 0
    result = db.execute(
        text(
            """
            UPDATE attachment_processing_jobs
            SET status = 'cancelled',
                finished_at = UTC_TIMESTAMP(),
                locked_by = NULL,
                locked_at = NULL,
                error_message = 'attachment deleted'
            WHERE attachment_id IN :aids
              AND status IN ('pending', 'processing')
            """
        ).bindparams(bindparam("aids", expanding=True)),
        {"aids": aids},
    )
    return int(result.rowcount or 0)


def delete_attachment_knowledge(
    db: Session,
    *,
    user_id: int,
    attachment_ids: Sequence[int],
) -> None:
    """Remove knowledge rows for attachment sources owned by user_id."""
    aids = _unique_positive_ids(attachment_ids)
    if not aids:
        return
    params = {"uid": int(user_id), "aids": aids}
    db.execute(
        text(
            """
            DELETE ke FROM knowledge_embeddings ke
            JOIN knowledge_chunks kc ON kc.id = ke.chunk_id
            JOIN knowledge_sources ks ON ks.id = kc.source_id
            WHERE ks.user_id = :uid
              AND ks.source_type = 'attachment'
              AND ks.source_id IN :aids
            """
        ).bindparams(bindparam("aids", expanding=True)),
        params,
    )
    db.execute(
        text(
            """
            DELETE kc FROM knowledge_chunks kc
            JOIN knowledge_sources ks ON ks.id = kc.source_id
            WHERE ks.user_id = :uid
              AND ks.source_type = 'attachment'
              AND ks.source_id IN :aids
            """
        ).bindparams(bindparam("aids", expanding=True)),
        params,
    )
    db.execute(
        text(
            """
            DELETE FROM knowledge_sources
            WHERE user_id = :uid
              AND source_type = 'attachment'
              AND source_id IN :aids
            """
        ).bindparams(bindparam("aids", expanding=True)),
        params,
    )


def delete_stored_files_best_effort(storage_paths: Sequence[str]) -> None:
    """Delete files after DB commit. Failures are logged without absolute paths."""
    for raw in storage_paths:
        path = (raw or "").strip()
        if not path:
            continue
        try:
            delete_stored_file(path)
        except Exception:
            logger.warning(
                "attachment_file_delete_failed storage_basename=%s",
                path.replace("\\", "/").rsplit("/", 1)[-1][:120],
            )
