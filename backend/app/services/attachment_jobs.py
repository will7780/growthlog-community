"""
Attachment processing job queue service.

If migration 014 has not been applied, helpers return safely so the main app
and worker can fall back to entry_attachments.status scanning.
"""
from __future__ import annotations

import logging
import os
import threading
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, bindparam, inspect, or_, text
from sqlalchemy.orm import Session

from app.models import AttachmentProcessingJob, EntryAttachment
from app.services.db_retry import is_retryable_db_error, with_session_retry
from app.timeutil import now_local

logger = logging.getLogger(__name__)

JOB_TABLE = "attachment_processing_jobs"

_fallback_semaphore: threading.BoundedSemaphore | None = None
_fallback_semaphore_lock = threading.Lock()


def has_attachment_jobs_table(db: Session) -> bool:
    try:
        return JOB_TABLE in set(inspect(db.bind).get_table_names())
    except Exception as exc:
        logger.warning("failed to inspect %s table: %s", JOB_TABLE, exc)
        return False


def make_fallback_worker_id() -> str:
    """Unique owner token per fallback invocation (never reuse upload-fallback)."""
    return f"upload-fallback:{os.getpid()}:{uuid.uuid4().hex[:12]}"


def _get_fallback_semaphore() -> threading.BoundedSemaphore:
    global _fallback_semaphore
    with _fallback_semaphore_lock:
        if _fallback_semaphore is None:
            from app.config import settings

            slots = max(1, int(settings.attachment_upload_fallback_max_concurrency))
            _fallback_semaphore = threading.BoundedSemaphore(slots)
        return _fallback_semaphore


def _acquire_fallback_slot() -> None:
    _get_fallback_semaphore().acquire()


def _release_fallback_slot() -> None:
    sem = _fallback_semaphore
    if sem is not None:
        sem.release()


def unfinished_job_attachment_ids_query(db: Session):
    """Subquery of attachment IDs that still have unfinished or retryable jobs."""
    if not has_attachment_jobs_table(db):
        return None

    return (
        db.query(AttachmentProcessingJob.attachment_id)
        .filter(
            or_(
                AttachmentProcessingJob.status.in_(("pending", "processing")),
                and_(
                    AttachmentProcessingJob.status == "failed",
                    AttachmentProcessingJob.attempt_count < AttachmentProcessingJob.max_attempts,
                ),
            )
        )
        .distinct()
    )


def create_attachment_processing_job(
    db: Session,
    attachment: EntryAttachment,
    job_type: str = "extract_text",
    *,
    priority: int = 0,
    max_attempts: int = 3,
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[AttachmentProcessingJob]:
    """Enqueue a processing job, or return an existing unfinished job."""
    if not has_attachment_jobs_table(db):
        return None

    locked = db.execute(
        text("SELECT id FROM entry_attachments WHERE id = :attachment_id FOR UPDATE"),
        {"attachment_id": int(attachment.id)},
    ).fetchone()
    if not locked:
        return None

    existing = (
        db.query(AttachmentProcessingJob)
        .filter(
            AttachmentProcessingJob.attachment_id == attachment.id,
            AttachmentProcessingJob.job_type == job_type,
            AttachmentProcessingJob.status.in_(("pending", "processing")),
        )
        .first()
    )
    if existing:
        return existing

    job = AttachmentProcessingJob(
        user_id=attachment.user_id,
        attachment_id=attachment.id,
        entry_id=attachment.entry_id,
        job_type=job_type,
        status="pending",
        priority=priority,
        attempt_count=0,
        max_attempts=max_attempts,
        available_at=now_local(),
        metadata_json=metadata,
    )
    db.add(job)
    db.flush()
    return job


def _select_job_ids_for_update(
    db: Session,
    *,
    batch_size: int,
    now: datetime,
    cutoff: datetime,
    retry_failed: bool,
    stuck_minutes: int,
) -> List[int]:
    """Lock candidate job ids with SKIP LOCKED inside the current transaction."""
    selected: List[int] = []
    remaining = batch_size

    if remaining > 0:
        rows = db.execute(
            text(
                """
                SELECT id FROM attachment_processing_jobs
                WHERE status = 'pending' AND available_at <= :now
                ORDER BY priority DESC, created_at ASC
                LIMIT :limit
                FOR UPDATE SKIP LOCKED
                """
            ),
            {"now": now, "limit": remaining},
        ).fetchall()
        selected.extend(int(row[0]) for row in rows)
        remaining = batch_size - len(selected)

    if retry_failed and remaining > 0:
        rows = db.execute(
            text(
                """
                SELECT id FROM attachment_processing_jobs
                WHERE status = 'failed'
                  AND attempt_count < max_attempts
                  AND available_at <= :now
                ORDER BY created_at ASC
                LIMIT :limit
                FOR UPDATE SKIP LOCKED
                """
            ),
            {"now": now, "limit": remaining},
        ).fetchall()
        for row in rows:
            job_id = int(row[0])
            if job_id not in selected:
                selected.append(job_id)
        remaining = batch_size - len(selected)

    if stuck_minutes > 0 and remaining > 0:
        rows = db.execute(
            text(
                """
                SELECT id FROM attachment_processing_jobs
                WHERE status = 'processing'
                  AND locked_at IS NOT NULL
                  AND locked_at < :cutoff
                ORDER BY created_at ASC
                LIMIT :limit
                FOR UPDATE SKIP LOCKED
                """
            ),
            {"cutoff": cutoff, "limit": remaining},
        ).fetchall()
        for row in rows:
            job_id = int(row[0])
            if job_id not in selected:
                selected.append(job_id)

    return selected[:batch_size]


def _apply_job_claim(db: Session, job_ids: List[int], worker_id: str, now: datetime) -> None:
    if not job_ids:
        return
    stmt = (
        text(
            """
            UPDATE attachment_processing_jobs
            SET status = 'processing',
                locked_by = :worker_id,
                locked_at = :now,
                attempt_count = attempt_count + 1,
                error_message = NULL,
                started_at = COALESCE(started_at, :now)
            WHERE id IN :job_ids
            """
        ).bindparams(bindparam("job_ids", expanding=True))
    )
    db.execute(stmt, {"worker_id": worker_id, "now": now, "job_ids": job_ids})


def claim_attachment_jobs(
    db: Session,
    batch_size: int,
    worker_id: str,
    *,
    retry_failed: bool = False,
    stuck_minutes: int = 30,
) -> List[AttachmentProcessingJob]:
    """Atomically claim pending, due-retry, or stuck processing jobs."""
    if not has_attachment_jobs_table(db):
        return []

    now = now_local()
    cutoff = now - timedelta(minutes=stuck_minutes)

    def _claim_once() -> List[AttachmentProcessingJob]:
        db.rollback()
        job_ids = _select_job_ids_for_update(
            db,
            batch_size=batch_size,
            now=now,
            cutoff=cutoff,
            retry_failed=retry_failed,
            stuck_minutes=stuck_minutes,
        )
        if not job_ids:
            db.commit()
            return []

        _apply_job_claim(db, job_ids, worker_id, now)
        db.commit()
        return (
            db.query(AttachmentProcessingJob)
            .filter(AttachmentProcessingJob.id.in_(job_ids))
            .order_by(
                AttachmentProcessingJob.priority.desc(),
                AttachmentProcessingJob.created_at.asc(),
            )
            .all()
        )

    return with_session_retry(db=db, fn=_claim_once, operation="claim_attachment_jobs")


def try_claim_job_by_id(
    db: Session,
    job_id: int,
    worker_id: str,
    *,
    stuck_minutes: int = 30,
) -> Optional[AttachmentProcessingJob]:
    """Atomically claim one job by id; None if another worker already owns it."""
    if not has_attachment_jobs_table(db):
        return None

    now = now_local()
    cutoff = now - timedelta(minutes=stuck_minutes)

    def _claim_once() -> Optional[AttachmentProcessingJob]:
        db.rollback()
        row = db.execute(
            text(
                """
                SELECT id FROM attachment_processing_jobs
                WHERE id = :job_id
                  AND (
                    (status = 'pending' AND available_at <= :now)
                    OR (
                      status = 'failed'
                      AND attempt_count < max_attempts
                      AND available_at <= :now
                    )
                    OR (
                      status = 'processing'
                      AND locked_at IS NOT NULL
                      AND locked_at < :cutoff
                    )
                  )
                FOR UPDATE
                """
            ),
            {"job_id": int(job_id), "now": now, "cutoff": cutoff},
        ).fetchone()
        if not row:
            db.rollback()
            return None

        db.execute(
            text(
                """
                UPDATE attachment_processing_jobs
                SET status = 'processing',
                    locked_by = :worker_id,
                    locked_at = :now,
                    attempt_count = attempt_count + 1,
                    error_message = NULL,
                    started_at = COALESCE(started_at, :now)
                WHERE id = :job_id
                """
            ),
            {"job_id": int(job_id), "worker_id": worker_id, "now": now},
        )
        db.commit()
        return (
            db.query(AttachmentProcessingJob)
            .filter(AttachmentProcessingJob.id == int(job_id))
            .first()
        )

    return with_session_retry(db=db, fn=_claim_once, operation="try_claim_job_by_id")


def mark_job_succeeded(db: Session, job_id: int) -> None:
    def _mark() -> None:
        job = (
            db.query(AttachmentProcessingJob)
            .filter(AttachmentProcessingJob.id == int(job_id))
            .first()
        )
        if not job:
            return
        job.status = "succeeded"
        job.finished_at = now_local()
        job.locked_by = None
        job.locked_at = None
        job.error_message = None
        db.commit()

    with_session_retry(db=db, fn=_mark, operation="mark_job_succeeded")


def mark_job_failed(db: Session, job_id: int, error_message: str) -> None:
    safe_message = (error_message or "unknown error")[:1000]

    def _mark() -> None:
        job = (
            db.query(AttachmentProcessingJob)
            .filter(AttachmentProcessingJob.id == int(job_id))
            .first()
        )
        if not job:
            return
        now = now_local()
        job.error_message = safe_message
        job.locked_by = None
        job.locked_at = None

        if (job.attempt_count or 0) < (job.max_attempts or 3):
            backoff_minutes = min(30, 2 ** max(job.attempt_count or 1, 1))
            job.status = "pending"
            job.available_at = now + timedelta(minutes=backoff_minutes)
            job.finished_at = None
        else:
            job.status = "failed"
            job.finished_at = now
        db.commit()

    with_session_retry(db=db, fn=_mark, operation="mark_job_failed")


def mark_job_cancelled(db: Session, job_id: int, reason: str = "") -> None:
    def _mark() -> None:
        job = (
            db.query(AttachmentProcessingJob)
            .filter(AttachmentProcessingJob.id == int(job_id))
            .first()
        )
        if not job:
            return
        job.status = "cancelled"
        job.finished_at = now_local()
        job.locked_by = None
        job.locked_at = None
        if reason:
            job.error_message = reason[:1000]
        db.commit()

    with_session_retry(db=db, fn=_mark, operation="mark_job_cancelled")


def _apply_final_failure(db: Session, job: AttachmentProcessingJob, attachment: EntryAttachment, error: str) -> str:
    safe = error[:1000]
    now = now_local()
    job.status = "failed"
    job.finished_at = now
    job.error_message = safe
    job.locked_by = None
    job.locked_at = None
    attachment.status = "failed"
    attachment.error_message = safe
    db.commit()
    return "failed"


def _apply_pending_retry(
    db: Session,
    job: AttachmentProcessingJob,
    attachment: EntryAttachment,
    error: str,
) -> str:
    now = now_local()
    # Short backoff for transient DB contention so workers can reclaim promptly.
    backoff_seconds = min(60, 2 ** max(job.attempt_count or 1, 1))
    job.status = "pending"
    job.available_at = now + timedelta(seconds=backoff_seconds)
    job.finished_at = None
    job.error_message = error[:1000]
    job.locked_by = None
    job.locked_at = None
    attachment.status = "uploaded"
    attachment.error_message = None
    db.commit()
    return "pending"


def finalize_job_after_processing(
    db: Session,
    job_id: int,
    *,
    db_error_exhausted: bool = False,
    forced_error: str | None = None,
) -> str:
    """Align job and attachment to a consistent terminal or retryable state."""
    def _finalize() -> str:
        db.rollback()
        job = (
            db.query(AttachmentProcessingJob)
            .filter(AttachmentProcessingJob.id == int(job_id))
            .with_for_update()
            .first()
        )
        if not job or job.status in ("succeeded", "cancelled"):
            return "noop"

        attachment = (
            db.query(EntryAttachment)
            .filter(EntryAttachment.id == job.attachment_id)
            .with_for_update()
            .first()
        )

        if attachment is None:
            job.status = "cancelled"
            job.finished_at = now_local()
            job.locked_by = None
            job.locked_at = None
            job.error_message = "attachment deleted"
            db.commit()
            return "cancelled"

        if attachment.status == "indexed":
            job.status = "succeeded"
            job.finished_at = now_local()
            job.locked_by = None
            job.locked_at = None
            job.error_message = None
            db.commit()
            return "succeeded"

        error = (forced_error or attachment.error_message or "processing failed").strip()
        attempts_left = (job.attempt_count or 0) < (job.max_attempts or 3)

        if db_error_exhausted and attempts_left:
            return _apply_pending_retry(db, job, attachment, error or "database contention")

        if db_error_exhausted:
            return _apply_final_failure(db, job, attachment, error or "database contention")

        if attachment.status == "failed":
            return _apply_final_failure(db, job, attachment, error)

        if attempts_left:
            return _apply_pending_retry(db, job, attachment, error)

        return _apply_final_failure(db, job, attachment, error)

    return with_session_retry(db=db, fn=_finalize, operation="finalize_job_after_processing")


def run_attachment_processing(attachment_id: int) -> str:
    """Process one attachment in an isolated session.

    Returns: processed | failed | db_error
    """
    from app.database import SessionLocal
    from app.services.attachment_processor import process_attachment

    db = SessionLocal()
    try:
        try:
            process_attachment(db, attachment_id)
        except Exception as exc:
            if is_retryable_db_error(exc):
                return "db_error"
            return "failed"

        attachment = (
            db.query(EntryAttachment)
            .filter(EntryAttachment.id == int(attachment_id))
            .first()
        )
        if attachment and attachment.status == "indexed":
            return "processed"
        return "failed"
    finally:
        db.close()


def choose_upload_background_handler(
    *,
    job: Optional[AttachmentProcessingJob],
    attachment_id: int,
    fallback_enabled: bool,
) -> Optional[tuple[str, int]]:
    """Decide which BackgroundTasks handler to schedule after upload."""
    if job is None:
        return ("legacy", int(attachment_id))
    if fallback_enabled:
        return ("job_fallback", int(job.id))
    return None


def claim_legacy_attachment_ids(
    db: Session,
    batch_size: int,
    *,
    retry_failed: bool = False,
    stuck_minutes: int = 30,
    exclude_active_jobs: bool = False,
) -> List[int]:
    """Atomically claim legacy entry_attachments rows when jobs table is absent or idle."""
    now = now_local()
    cutoff = now - timedelta(minutes=stuck_minutes)
    statuses = ["uploaded"]
    if retry_failed:
        statuses.append("failed")

    def _claim_once() -> List[int]:
        db.rollback()
        blocked_ids: List[int] = []
        if exclude_active_jobs:
            blocked = unfinished_job_attachment_ids_query(db)
            if blocked is not None:
                blocked_ids = [int(row[0]) for row in blocked.all()]

        rows = db.execute(
            text(
                """
                SELECT id FROM entry_attachments
                WHERE status IN :statuses
                ORDER BY created_at ASC
                LIMIT :limit
                FOR UPDATE SKIP LOCKED
                """
            ).bindparams(bindparam("statuses", expanding=True)),
            {"statuses": statuses, "limit": batch_size},
        ).fetchall()
        selected = [int(row[0]) for row in rows if int(row[0]) not in blocked_ids]

        if stuck_minutes > 0 and len(selected) < batch_size:
            stuck_rows = db.execute(
                text(
                    """
                    SELECT id FROM entry_attachments
                    WHERE status = 'processing'
                      AND updated_at IS NOT NULL
                      AND updated_at < :cutoff
                    ORDER BY created_at ASC
                    LIMIT :limit
                    FOR UPDATE SKIP LOCKED
                    """
                ),
                {"cutoff": cutoff, "limit": batch_size - len(selected)},
            ).fetchall()
            for row in stuck_rows:
                attachment_id = int(row[0])
                if attachment_id not in selected and attachment_id not in blocked_ids:
                    selected.append(attachment_id)

        if not selected:
            db.commit()
            return []

        db.execute(
            text(
                """
                UPDATE entry_attachments
                SET status = 'processing', error_message = NULL, updated_at = :now
                WHERE id IN :ids
                """
            ).bindparams(bindparam("ids", expanding=True)),
            {"now": now, "ids": selected},
        )
        db.commit()
        return selected

    return with_session_retry(db=db, fn=_claim_once, operation="claim_legacy_attachment_ids")


def process_attachment_job_background(job_id: int) -> None:
    """Safe upload-time BackgroundTasks fallback for a processing job."""
    from app.database import SessionLocal

    _acquire_fallback_slot()
    try:
        owner = make_fallback_worker_id()
        claim_db = SessionLocal()
        try:
            if not has_attachment_jobs_table(claim_db):
                return

            job = try_claim_job_by_id(claim_db, int(job_id), owner)
            if not job:
                return
        finally:
            claim_db.close()

        outcome = run_attachment_processing(int(job.attachment_id))

        status_db = SessionLocal()
        try:
            finalize_job_after_processing(
                status_db,
                int(job_id),
                db_error_exhausted=(outcome == "db_error"),
            )
        except Exception:
            logger.exception("upload-fallback finalize failed job_id=%s", job_id)
        finally:
            status_db.close()
    except Exception:
        logger.exception("upload-fallback job background failed job_id=%s", job_id)
    finally:
        _release_fallback_slot()
