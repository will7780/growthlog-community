"""Independent Notion job claim/lease. Network I/O never holds a row lock."""
from __future__ import annotations

from datetime import timedelta
from threading import Event, Thread
from typing import Callable, List, Optional

from sqlalchemy import and_, bindparam, inspect, or_, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import NotionSyncJob
from app.services.db_retry import with_session_retry
from app.timeutil import now_local

JOB_TABLE = "notion_sync_jobs"
LEASE_SECONDS = 180
MAX_ATTEMPTS = 6
NOTION_BATCH_SIZE = 2


class NotionJobClaimLost(RuntimeError):
    code = "NOTION_JOB_CLAIM_LOST"


def has_notion_jobs_table(db: Session) -> bool:
    try:
        bind = db.connection()
        return JOB_TABLE in set(inspect(bind).get_table_names())
    except Exception:
        return False


def enqueue_job(
    db: Session,
    *,
    user_id: int,
    connection_id: int,
    job_type: str,
    notion_page_id: Optional[int] = None,
    provider_event_id: Optional[str] = None,
) -> Optional[NotionSyncJob]:
    if not has_notion_jobs_table(db):
        return None
    if provider_event_id:
        existing_event = (
            db.query(NotionSyncJob)
            .filter(NotionSyncJob.provider_event_id == provider_event_id)
            .first()
        )
        if existing_event is not None:
            return existing_event
    existing = (
        db.query(NotionSyncJob)
        .filter(
            NotionSyncJob.connection_id == int(connection_id),
            NotionSyncJob.job_type == job_type,
            NotionSyncJob.notion_page_id == notion_page_id,
            NotionSyncJob.status.in_(("pending", "processing", "retry")),
        )
        .first()
    )
    if existing is not None:
        return existing
    job = NotionSyncJob(
        user_id=int(user_id),
        connection_id=int(connection_id),
        notion_page_id=notion_page_id,
        job_type=job_type,
        status="pending",
        provider_event_id=provider_event_id,
        available_at=now_local(),
    )
    db.add(job)
    db.flush()
    return job


def _claim_lock_sql(db: Session) -> str:
    dialect = str(getattr(getattr(db.bind, "dialect", None), "name", "") or "")
    if dialect.startswith("mysql"):
        return " FOR UPDATE SKIP LOCKED"
    return ""


def claim_notion_jobs(db: Session, batch_size: int, worker_id: str) -> List[NotionSyncJob]:
    if not has_notion_jobs_table(db):
        return []
    now = now_local()

    def _claim_once() -> List[NotionSyncJob]:
        db.rollback()
        rows = db.execute(
            text(
                f"""
                SELECT id FROM notion_sync_jobs
                WHERE (
                    status IN ('pending', 'retry') AND available_at <= :now
                ) OR (
                    status = 'processing'
                    AND lease_until IS NOT NULL
                    AND lease_until < :now
                )
                ORDER BY created_at ASC, id ASC
                LIMIT :limit
                {_claim_lock_sql(db)}
                """
            ),
            {"now": now, "limit": int(batch_size)},
        ).fetchall()
        job_ids = [int(row[0]) for row in rows]
        if not job_ids:
            db.commit()
            return []
        lease = now + timedelta(seconds=LEASE_SECONDS)
        stmt = (
            text(
                """
                UPDATE notion_sync_jobs
                SET status = 'processing',
                    lock_owner = :worker_id,
                    locked_at = :now,
                    lease_until = :lease,
                    attempt_count = attempt_count + 1,
                    last_error_code = NULL
                WHERE id IN :job_ids
                """
            ).bindparams(bindparam("job_ids", expanding=True))
        )
        db.execute(stmt, {"worker_id": worker_id, "now": now, "lease": lease, "job_ids": job_ids})
        db.commit()
        return (
            db.query(NotionSyncJob)
            .filter(NotionSyncJob.id.in_(job_ids))
            .order_by(NotionSyncJob.created_at.asc())
            .all()
        )

    return with_session_retry(db=db, fn=_claim_once, operation="claim_notion_jobs")


def claim_is_active(db: Session, job_id: int, worker_id: str) -> bool:
    now = now_local()
    row = (
        db.query(NotionSyncJob.id)
        .filter(
            NotionSyncJob.id == int(job_id),
            NotionSyncJob.lock_owner == worker_id,
            NotionSyncJob.status == "processing",
            NotionSyncJob.lease_until.isnot(None),
            NotionSyncJob.lease_until > now,
        )
        .first()
    )
    return row is not None


def require_active_claim(db: Session, job_id: int, worker_id: str) -> None:
    if not claim_is_active(db, job_id, worker_id):
        raise NotionJobClaimLost(NotionJobClaimLost.code)


def renew_lease(db: Session, job_id: int, worker_id: str) -> bool:
    now = now_local()
    result = db.execute(
        text(
            """
            UPDATE notion_sync_jobs
            SET lease_until = :lease, locked_at = :now
            WHERE id = :job_id
              AND lock_owner = :worker_id
              AND status = 'processing'
              AND lease_until IS NOT NULL
              AND lease_until > :now
            """
        ),
        {
            "lease": now + timedelta(seconds=LEASE_SECONDS),
            "now": now,
            "job_id": int(job_id),
            "worker_id": worker_id,
        },
    )
    db.commit()
    return int(result.rowcount or 0) == 1


def finalize_job(
    db: Session,
    job_id: int,
    *,
    worker_id: str,
    ok: bool,
    error_code: Optional[str] = None,
) -> bool:
    now = now_local()
    job = (
        db.query(NotionSyncJob)
        .filter(
            NotionSyncJob.id == int(job_id),
            NotionSyncJob.lock_owner == worker_id,
            NotionSyncJob.status == "processing",
            NotionSyncJob.lease_until.isnot(None),
            NotionSyncJob.lease_until > now,
        )
        .with_for_update()
        .first()
    )
    if job is None:
        db.rollback()
        return False
    if ok:
        job.status = "succeeded"
        job.lock_owner = None
        job.locked_at = None
        job.lease_until = None
        job.last_error_code = None
    elif int(job.attempt_count or 0) < MAX_ATTEMPTS:
        job.status = "retry"
        job.lock_owner = None
        job.locked_at = None
        job.lease_until = None
        job.available_at = now_local() + timedelta(seconds=min(300, 15 * (2 ** max(0, int(job.attempt_count) - 1))))
        job.last_error_code = error_code
    else:
        job.status = "failed"
        job.lock_owner = None
        job.locked_at = None
        job.lease_until = None
        job.last_error_code = error_code
    db.add(job)
    db.commit()
    return True


class NotionJobLeaseHeartbeat:
    """Renew a job lease with short-lived sessions while provider I/O runs."""

    def __init__(
        self,
        *,
        job_id: int,
        worker_id: str,
        session_factory: Callable[[], Session] = SessionLocal,
        interval_seconds: Optional[float] = None,
    ):
        self.job_id = int(job_id)
        self.worker_id = worker_id
        self.session_factory = session_factory
        self.interval_seconds = float(interval_seconds or max(5, LEASE_SECONDS // 3))
        self._stop = Event()
        self._lost = Event()
        self._thread: Optional[Thread] = None

    def _renew_once(self) -> bool:
        db = self.session_factory()
        try:
            return renew_lease(db, self.job_id, self.worker_id)
        except SQLAlchemyError:
            db.rollback()
            return False
        finally:
            db.close()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            if not self._renew_once():
                self._lost.set()
                return

    def __enter__(self) -> "NotionJobLeaseHeartbeat":
        self._thread = Thread(
            target=self._run,
            name=f"notion-job-lease-{self.job_id}",
            daemon=True,
        )
        self._thread.start()
        return self

    def assert_owned(self) -> None:
        if self._lost.is_set():
            raise NotionJobClaimLost(NotionJobClaimLost.code)

    def __exit__(self, exc_type, exc, traceback) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.interval_seconds + 1.0))
        self.assert_owned()
