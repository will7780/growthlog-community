"""
Standalone attachment processing worker.

Preferred (container /app working directory):
  python -m scripts.process_attachments --loop --interval 30 --retry-failed

Also supported:
  python scripts/process_attachments.py --batch-size 1
"""
from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import time
from pathlib import Path
from typing import Dict

# Allow `python scripts/process_attachments.py` when PYTHONPATH is unset.
_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.database import SessionLocal
from app.services.attachment_jobs import (
    claim_attachment_jobs,
    claim_legacy_attachment_ids,
    finalize_job_after_processing,
    has_attachment_jobs_table,
    run_attachment_processing,
)
from app.services.notion_errors import NotionServiceError
from app.services.notion_jobs import (
    NOTION_BATCH_SIZE,
    NotionJobClaimLost,
    NotionJobLeaseHeartbeat,
    claim_notion_jobs,
    finalize_job,
    has_notion_jobs_table,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("attachment_worker")


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _empty_stats() -> Dict[str, int]:
    return {
        "jobs_claimed": 0,
        "jobs_succeeded": 0,
        "jobs_failed": 0,
        "jobs_pending": 0,
        "claimed": 0,
        "processed": 0,
        "failed": 0,
        "skipped": 0,
        "notion_claimed": 0,
        "notion_succeeded": 0,
        "notion_failed": 0,
    }


def _process_legacy_batch(
    batch_size: int,
    retry_failed: bool,
    stuck_minutes: int,
    *,
    exclude_active_jobs: bool = False,
) -> Dict[str, int]:
    stats = _empty_stats()
    claim_db = SessionLocal()
    try:
        attachment_ids = claim_legacy_attachment_ids(
            claim_db,
            batch_size,
            retry_failed=retry_failed,
            stuck_minutes=stuck_minutes,
            exclude_active_jobs=exclude_active_jobs,
        )
    finally:
        claim_db.close()

    stats["claimed"] = len(attachment_ids)
    for attachment_id in attachment_ids:
        logger.info("processing attachment %s (legacy)", attachment_id)
        outcome = run_attachment_processing(attachment_id)
        if outcome == "processed":
            stats["processed"] += 1
        elif outcome == "failed":
            stats["failed"] += 1
        else:
            stats["skipped"] += 1
    return stats


def _process_jobs_batch(
    batch_size: int,
    worker_id: str,
    retry_failed: bool,
    stuck_minutes: int,
) -> Dict[str, int]:
    stats = _empty_stats()
    claim_db = SessionLocal()
    try:
        if not has_attachment_jobs_table(claim_db):
            return stats
        jobs = claim_attachment_jobs(
            claim_db,
            batch_size,
            worker_id,
            retry_failed=retry_failed,
            stuck_minutes=stuck_minutes,
        )
    finally:
        claim_db.close()

    stats["jobs_claimed"] = len(jobs)
    for job in jobs:
        job_id = int(job.id)
        attachment_id = int(job.attachment_id)
        logger.info("processing job %s attachment %s", job_id, attachment_id)
        outcome = run_attachment_processing(attachment_id)

        status_db = SessionLocal()
        try:
            result = finalize_job_after_processing(
                status_db,
                job_id,
                db_error_exhausted=(outcome == "db_error"),
            )
            if result == "succeeded":
                stats["jobs_succeeded"] += 1
            elif result == "pending":
                stats["jobs_pending"] += 1
            elif result in ("failed", "cancelled"):
                stats["jobs_failed"] += 1
                logger.warning("job %s finalize=%s attachment=%s", job_id, result, attachment_id)
        finally:
            status_db.close()

        if outcome == "failed":
            stats["failed"] += 1
        elif outcome == "processed":
            stats["processed"] += 1
    return stats


def _process_notion_batch(worker_id: str) -> Dict[str, int]:
    stats = {"notion_claimed": 0, "notion_succeeded": 0, "notion_failed": 0}
    from app.config import settings

    if not settings.notion_integration_enabled or not settings.notion_sync_enabled:
        return stats
    claim_db = SessionLocal()
    try:
        if not has_notion_jobs_table(claim_db):
            return stats
        jobs = claim_notion_jobs(claim_db, NOTION_BATCH_SIZE, worker_id)
    finally:
        claim_db.close()
    stats["notion_claimed"] = len(jobs)
    from app.services.notion_sync import process_claimed_job

    for job in jobs:
        job_id = int(job.id)
        try:
            with NotionJobLeaseHeartbeat(job_id=job_id, worker_id=worker_id) as lease:
                outcome = process_claimed_job(
                    job_id,
                    worker_id=worker_id,
                    claim_check=lease.assert_owned,
                )
                lease.assert_owned()
            status_db = SessionLocal()
            try:
                finalized = finalize_job(
                    status_db,
                    job_id,
                    worker_id=worker_id,
                    ok=outcome == "processed",
                )
            finally:
                status_db.close()
            if not finalized:
                raise NotionJobClaimLost(NotionJobClaimLost.code)
            if outcome == "processed":
                stats["notion_succeeded"] += 1
            else:
                stats["notion_failed"] += 1
        except NotionJobClaimLost:
            stats["notion_failed"] += 1
            logger.warning("notion job %s claim lost", job_id)
        except NotionServiceError as exc:
            status_db = SessionLocal()
            try:
                finalize_job(status_db, job_id, worker_id=worker_id, ok=False, error_code=exc.code)
            finally:
                status_db.close()
            stats["notion_failed"] += 1
            logger.warning("notion job %s failed code=%s", job_id, exc.code)
        except Exception:
            status_db = SessionLocal()
            try:
                finalize_job(
                    status_db,
                    job_id,
                    worker_id=worker_id,
                    ok=False,
                    error_code="NOTION_INTERNAL_ERROR",
                )
            finally:
                status_db.close()
            stats["notion_failed"] += 1
            logger.warning("notion job %s failed", job_id)
    return stats


def process_batch(batch_size: int, retry_failed: bool, stuck_minutes: int) -> Dict[str, int]:
    worker_id = _worker_id()
    job_stats = _process_jobs_batch(batch_size, worker_id, retry_failed, stuck_minutes)
    if job_stats["jobs_claimed"] > 0:
        notion_stats = _process_notion_batch(worker_id)
        job_stats.update(notion_stats)
        return job_stats

    probe_db = SessionLocal()
    try:
        jobs_table_exists = has_attachment_jobs_table(probe_db)
    finally:
        probe_db.close()

    # Jobs table missing: scan entry_attachments.status (legacy path).
    # Jobs table present but idle: only claim attachments without active jobs.
    legacy_stats = _process_legacy_batch(
        batch_size,
        retry_failed,
        stuck_minutes,
        exclude_active_jobs=jobs_table_exists,
    )
    legacy_stats["jobs_claimed"] = job_stats["jobs_claimed"]
    legacy_stats["jobs_succeeded"] = job_stats["jobs_succeeded"]
    legacy_stats["jobs_failed"] = job_stats["jobs_failed"]
    legacy_stats["jobs_pending"] = job_stats["jobs_pending"]
    notion_stats = _process_notion_batch(worker_id)
    legacy_stats.update(notion_stats)
    return legacy_stats


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--stuck-minutes", type=int, default=30)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=int, default=30)
    args = parser.parse_args()

    while True:
        stats = process_batch(args.batch_size, args.retry_failed, args.stuck_minutes)
        logger.info(
            "batch complete: jobs_claimed=%s jobs_succeeded=%s jobs_failed=%s jobs_pending=%s "
            "claimed=%s processed=%s failed=%s skipped=%s notion_claimed=%s notion_succeeded=%s notion_failed=%s",
            stats["jobs_claimed"],
            stats["jobs_succeeded"],
            stats["jobs_failed"],
            stats["jobs_pending"],
            stats["claimed"],
            stats["processed"],
            stats["failed"],
            stats["skipped"],
            stats.get("notion_claimed", 0),
            stats.get("notion_succeeded", 0),
            stats.get("notion_failed", 0),
        )
        if not args.loop:
            break
        time.sleep(max(args.interval, 1))


if __name__ == "__main__":
    main()
