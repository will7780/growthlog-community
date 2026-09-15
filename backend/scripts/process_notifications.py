"""
Standalone notification outbox worker.

  python -m scripts.process_notifications --loop --interval 60
"""
from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import time
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.config import settings
from app.database import SessionLocal
from app.services.notification_service import (
    claim_outbox_jobs,
    enqueue_due_notifications,
    process_one_outbox,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("notification_worker")


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def run_once(*, batch_size: int, enqueue: bool) -> dict[str, int]:
    stats = {"enqueued": 0, "claimed": 0, "sent": 0, "failed": 0, "cancelled": 0, "retry": 0}
    if enqueue:
        db = SessionLocal()
        try:
            stats["enqueued"] = enqueue_due_notifications(db)
        finally:
            db.close()

    claim_db = SessionLocal()
    try:
        ids = claim_outbox_jobs(claim_db, worker_id=_worker_id(), batch_size=batch_size)
    finally:
        claim_db.close()

    stats["claimed"] = len(ids)
    for outbox_id in ids:
        db = SessionLocal()
        try:
            outcome = process_one_outbox(db, outbox_id)
            stats[outcome] = stats.get(outcome, 0) + 1
            logger.info("outbox id=%s outcome=%s", outbox_id, outcome)
        finally:
            db.close()
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="GrowthLog notification worker")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--no-enqueue", action="store_true")
    args = parser.parse_args()

    if not settings.notification_worker_enabled and args.loop:
        logger.warning("NOTIFICATION_WORKER_ENABLED=false; exiting")
        return 0

    enqueue = not args.no_enqueue
    if args.loop:
        while True:
            stats = run_once(batch_size=args.batch_size, enqueue=enqueue)
            logger.info("tick %s", stats)
            time.sleep(max(5, int(args.interval)))
    stats = run_once(batch_size=args.batch_size, enqueue=enqueue)
    logger.info("done %s", stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
