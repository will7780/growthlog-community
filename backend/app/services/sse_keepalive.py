"""Transport keepalive wrapper for long-running SSE generators."""
from __future__ import annotations

import asyncio
import contextlib
from typing import AsyncIterator

SSE_KEEPALIVE_COMMENT = ": keepalive\n\n"


async def iter_sse_with_keepalive(
    source: AsyncIterator[str],
    *,
    interval_seconds: float,
) -> AsyncIterator[str]:
    """Yield source frames and an SSE comment while the next frame is pending.

    Comments keep the public HTTP path active without entering the application
    event protocol. Cancellation is propagated into the pending source read so
    request claims and provider streams can release their resources normally.
    """
    interval = float(interval_seconds)
    if interval <= 0:
        raise ValueError("sse_keepalive_interval_invalid")

    iterator = source.__aiter__()
    pending: asyncio.Task[str] | None = None
    try:
        pending = asyncio.create_task(anext(iterator))
        while True:
            done, _ = await asyncio.wait({pending}, timeout=interval)
            if not done:
                yield SSE_KEEPALIVE_COMMENT
                continue

            try:
                frame = pending.result()
            except StopAsyncIteration:
                return

            yield frame
            pending = asyncio.create_task(anext(iterator))
    except asyncio.CancelledError:
        if pending is not None and not pending.done():
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pending
        raise
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pending
        close = getattr(iterator, "aclose", None)
        if callable(close):
            with contextlib.suppress(Exception):
                await close()
