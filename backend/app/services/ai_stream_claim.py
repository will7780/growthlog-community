"""
R11.3-Fix2/Fix3: durable stream claim lifecycle.

- Acquire / heartbeat / atomic finalize / independent release.
- Heartbeat and cancel-release use short-lived sessions on the same engine as
  the request Session (never share the request Session; never hold row locks
  across LLM I/O).
- Lost ownership sets an asyncio.Event so providers with no deltas are cancelled.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any, AsyncIterator, Optional

from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.services.ai_stream_idempotency import (
    STREAM_CLAIM_LOST,
    StreamIdempotencyError,
    claim_lease_seconds,
    finalize_stream_turn_with_claim,
    release_stream_claim,
    try_acquire_stream_claim,
)

logger = logging.getLogger(__name__)

__all__ = [
    "STREAM_CLAIM_LOST",
    "StreamClaimGuard",
    "claim_heartbeat_seconds",
    "heartbeat_stream_claim",
    "release_stream_claim_independent",
    "claims_exist_for_session",
    "delete_claims_for_session",
]


def claim_heartbeat_seconds() -> float:
    lease = float(claim_lease_seconds())
    configured = float(getattr(settings, "stream_claim_heartbeat_seconds", 30.0) or 30.0)
    # Renew well before expiry; never exceed lease/3. Floor 0.2 for Event-barrier tests.
    return max(0.2, min(configured, lease / 3.0))


def _short_session(engine: Any) -> Session:
    return sessionmaker(bind=engine)()


def heartbeat_stream_claim(
    *,
    user_id: int,
    session_id: str,
    request_id: str,
    owner_token: str,
    engine: Any = None,
) -> bool:
    """
    Extend lease with a short-lived Session.
    Returns True iff exactly one claimed row owned by owner_token was updated.
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import text

    from app.database import SessionLocal
    from app.services.ai_stream_idempotency import (
        CLAIM_STATUS_CLAIMED,
        _claims_table_ready,
    )

    db = _short_session(engine) if engine is not None else SessionLocal()
    try:
        if not _claims_table_ready(db):
            return True
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        lease = now + timedelta(seconds=claim_lease_seconds())
        result = db.execute(
            text(
                "UPDATE ai_stream_request_claims "
                "SET lease_until=:lease, updated_at=UTC_TIMESTAMP(6) "
                "WHERE user_id=:uid AND session_id=:sid AND request_id=:rid "
                "AND owner_token=:owner AND status=:st"
            ),
            {
                "lease": lease,
                "uid": int(user_id),
                "sid": str(session_id),
                "rid": str(request_id),
                "owner": owner_token,
                "st": CLAIM_STATUS_CLAIMED,
            },
        )
        db.commit()
        return int(getattr(result, "rowcount", 0) or 0) == 1
    except Exception:
        db.rollback()
        logger.warning("stream claim heartbeat failed", exc_info=False)
        return False
    finally:
        db.close()


def release_stream_claim_independent(
    *,
    user_id: int,
    session_id: str,
    request_id: str,
    owner_token: Optional[str],
    engine: Any = None,
) -> None:
    """
    Release via a short-lived Session (never the request Session).
    When engine is provided, uses that bind (same DB as the request).
    """
    if not owner_token:
        return
    from app.database import SessionLocal

    db = _short_session(engine) if engine is not None else SessionLocal()
    try:
        release_stream_claim(
            db,
            user_id=user_id,
            session_id=session_id,
            request_id=request_id,
            owner_token=owner_token,
        )
    finally:
        db.close()


class StreamClaimGuard:
    """
    Async claim lifecycle for one streaming generation.

    Usage:
        async with StreamClaimGuard(...) as guard:
            if guard.replay: ...; return
            async for kind, chunk in guard.iter_provider(stream_chat_completion(...)):
                ...
            saved = guard.finalize_turn(...)
            yield done  # only after finalize succeeds
    """

    def __init__(
        self,
        db: Session,
        *,
        user_id: int,
        session_id: str,
        request_id: str,
        message_text: str,
    ):
        self.db = db
        self.user_id = int(user_id)
        self.session_id = str(session_id)
        self.request_id = str(request_id)
        self.message_text = message_text
        self.owner_token: Optional[str] = None
        self.replay = False
        self._completed = False
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._lost = False
        self._lost_event = asyncio.Event()
        self._engine = db.get_bind()

    @property
    def lost(self) -> bool:
        return self._lost

    @property
    def completed(self) -> bool:
        return self._completed

    def wait_lost(self) -> asyncio.Event:
        return self._lost_event

    def _mark_lost(self) -> None:
        self._lost = True
        self._lost_event.set()

    async def __aenter__(self) -> "StreamClaimGuard":
        status, owner = try_acquire_stream_claim(
            self.db,
            user_id=self.user_id,
            session_id=self.session_id,
            request_id=self.request_id,
            message_text=self.message_text,
        )
        if status == "completed":
            self.replay = True
            return self
        self.owner_token = owner
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        await self._stop_heartbeat()
        if self._completed:
            return False
        if self.owner_token and not self.replay:
            try:
                await asyncio.to_thread(
                    release_stream_claim_independent,
                    user_id=self.user_id,
                    session_id=self.session_id,
                    request_id=self.request_id,
                    owner_token=self.owner_token,
                    engine=self._engine,
                )
            except Exception:
                logger.warning("independent claim release failed", exc_info=False)
        return False

    async def _heartbeat_loop(self) -> None:
        interval = claim_heartbeat_seconds()
        try:
            while True:
                await asyncio.sleep(interval)
                if self._completed or self.owner_token is None:
                    return
                ok = await asyncio.to_thread(
                    heartbeat_stream_claim,
                    user_id=self.user_id,
                    session_id=self.session_id,
                    request_id=self.request_id,
                    owner_token=self.owner_token,
                    engine=self._engine,
                )
                if not ok:
                    self._mark_lost()
                    return
        except asyncio.CancelledError:
            return

    async def _stop_heartbeat(self) -> None:
        task = self._heartbeat_task
        self._heartbeat_task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    def ensure_owned(self) -> None:
        if self._lost:
            raise StreamIdempotencyError(
                STREAM_CLAIM_LOST,
                "生成请求所有权已失效，请重试。",
            )

    async def await_provider(self, awaitable: Any) -> Any:
        """Await one provider call while cancelling it immediately if the claim is lost."""
        self.ensure_owned()
        work_task = asyncio.ensure_future(awaitable)
        lost_task = asyncio.create_task(self._lost_event.wait())
        try:
            done, _pending = await asyncio.wait(
                {work_task, lost_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if self._lost or lost_task in done:
                work_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await work_task
                raise StreamIdempotencyError(
                    STREAM_CLAIM_LOST,
                    "生成请求所有权已失效，请重试。",
                )
            lost_task.cancel()
            with suppress(asyncio.CancelledError):
                await lost_task
            return work_task.result()
        except asyncio.CancelledError:
            work_task.cancel()
            lost_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await work_task
            with suppress(asyncio.CancelledError, Exception):
                await lost_task
            raise
        finally:
            if not lost_task.done():
                lost_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await lost_task
    async def iter_provider(self, agen: Any) -> AsyncIterator[Any]:
        """Yield from provider until lost; cancel provider even with no deltas."""
        it = agen.__aiter__()
        while True:
            self.ensure_owned()
            next_task = asyncio.create_task(it.__anext__())
            lost_task = asyncio.create_task(self._lost_event.wait())
            try:
                done, pending = await asyncio.wait(
                    {next_task, lost_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                    with suppress(asyncio.CancelledError, StopAsyncIteration, Exception):
                        await task

                if self._lost or lost_task in done:
                    if hasattr(agen, "aclose"):
                        with suppress(Exception):
                            await agen.aclose()
                    raise StreamIdempotencyError(
                        STREAM_CLAIM_LOST,
                        "生成请求所有权已失效，请重试。",
                    )

                try:
                    item = next_task.result()
                except StopAsyncIteration:
                    return
                yield item
            except StreamIdempotencyError:
                raise
            except asyncio.CancelledError:
                if hasattr(agen, "aclose"):
                    with suppress(Exception):
                        await agen.aclose()
                raise

    def finalize_turn(
        self,
        *,
        user_content: str,
        assistant_content: str,
        assistant_references: Optional[list] = None,
        source_set_version: Optional[int] = None,
        proposal_id: Optional[int] = None,
        persist_user: bool = True,
        result_fingerprint: Optional[str] = None,
    ) -> dict:
        """Atomically save messages + mark claim completed. Sets _completed only on success."""
        self.ensure_owned()
        if self.replay:
            self._completed = True
            raise StreamIdempotencyError(
                "STREAM_PROTOCOL_ERROR",
                "流式协议异常，请重试。",
            )
        saved = finalize_stream_turn_with_claim(
            self.db,
            user_id=self.user_id,
            session_id=self.session_id,
            request_id=self.request_id,
            owner_token=self.owner_token,
            user_content=user_content,
            assistant_content=assistant_content,
            assistant_references=assistant_references,
            source_set_version=source_set_version,
            proposal_id=proposal_id,
            persist_user=persist_user,
            result_fingerprint=result_fingerprint,
        )
        self._completed = True
        return saved


def claims_exist_for_session(db: Session, user_id: int, session_id: str) -> bool:
    from sqlalchemy import text

    from app.services.ai_stream_idempotency import _claims_table_ready

    if not _claims_table_ready(db):
        return False
    n = db.execute(
        text(
            "SELECT COUNT(*) FROM ai_stream_request_claims "
            "WHERE user_id=:uid AND session_id=:sid"
        ),
        {"uid": int(user_id), "sid": str(session_id)},
    ).scalar()
    return int(n or 0) > 0


def delete_claims_for_session(db: Session, user_id: int, session_id: str) -> None:
    """Delete claim rows for session inside caller's open transaction (no commit)."""
    from sqlalchemy import text

    from app.services.ai_stream_idempotency import _claims_table_ready

    if not _claims_table_ready(db):
        return
    db.execute(
        text(
            "DELETE FROM ai_stream_request_claims "
            "WHERE user_id=:uid AND session_id=:sid"
        ),
        {"uid": int(user_id), "sid": str(session_id)},
    )
