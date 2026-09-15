"""
MySQL transient error retry helpers (deadlock / lock wait timeout).

Only SQLAlchemy DBAPIError/OperationalError and known DB driver exceptions
(or their nested cause/context / .orig) may trigger retry. Arbitrary
application exceptions are never classified by message text alone.
"""
from __future__ import annotations

import logging
import random
import re
import time
from typing import Callable, Iterator, Optional, TypeVar

from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

T = TypeVar("T")

RETRYABLE_MYSQL_ERRNOS = {1213, 1205}
RETRYABLE_SQLSTATE = {"40001"}
_DRIVER_MODULE_PREFIXES = ("pymysql", "MySQLdb", "mysql.connector", "mysqldb")
_RETRYABLE_MESSAGE_RE = re.compile(
    r"(?:\b1213\b|\b1205\b|Deadlock found|lock wait timeout|SQLSTATE\[40001\])",
    re.IGNORECASE,
)


def _iter_exception_chain(exc: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        cur = stack.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        yield cur
        for attr in ("__cause__", "__context__"):
            nested = getattr(cur, attr, None)
            if isinstance(nested, BaseException):
                stack.append(nested)


def _is_driver_exception(exc: BaseException) -> bool:
    module = type(exc).__module__ or ""
    return module.startswith(_DRIVER_MODULE_PREFIXES)


def _is_sqlalchemy_db_error(exc: BaseException) -> bool:
    return isinstance(exc, (DBAPIError, OperationalError))


def _match_errno_or_sqlstate(obj: object | None) -> bool:
    if obj is None:
        return False
    args = getattr(obj, "args", ()) or ()
    if args:
        try:
            if int(args[0]) in RETRYABLE_MYSQL_ERRNOS:
                return True
        except (TypeError, ValueError):
            pass
        if isinstance(args[0], str) and args[0] in RETRYABLE_SQLSTATE:
            return True
    sqlstate = getattr(obj, "sqlstate", None)
    if isinstance(sqlstate, str) and sqlstate in RETRYABLE_SQLSTATE:
        return True
    return False


def _match_db_error_text(exc: BaseException) -> bool:
    """Message match is allowed only for confirmed DB exception nodes."""
    return bool(_RETRYABLE_MESSAGE_RE.search(str(exc)))


def is_retryable_db_error(exc: BaseException) -> bool:
    """Return True only for MySQL deadlock / lock-wait timeout DB errors."""
    for cur in _iter_exception_chain(exc):
        if _is_sqlalchemy_db_error(cur):
            if _match_errno_or_sqlstate(cur):
                return True
            if _match_errno_or_sqlstate(getattr(cur, "orig", None)):
                return True
            if _match_db_error_text(cur):
                return True
            continue

        if _is_driver_exception(cur):
            if _match_errno_or_sqlstate(cur) or _match_db_error_text(cur):
                return True
            continue

        # Non-exception .orig payload on a SQLAlchemy wrapper already handled above.
        # Ignore plain application exceptions even if their text mentions 1213/deadlock.

    return False


def with_db_retry(
    fn: Callable[[], T],
    *,
    max_attempts: int = 3,
    base_delay: float = 0.05,
    max_delay: float = 1.0,
    operation: str = "db",
    on_retry: Optional[Callable[[], None]] = None,
) -> T:
    """Run *fn* with bounded exponential backoff on retryable DB errors."""
    attempt = 0
    while True:
        attempt += 1
        try:
            return fn()
        except Exception as exc:
            if not is_retryable_db_error(exc) or attempt >= max_attempts:
                raise
            if on_retry is not None:
                on_retry()
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            delay += random.uniform(0, delay * 0.25)
            logger.warning(
                "%s retryable db error attempt=%s/%s delay=%.3fs type=%s",
                operation,
                attempt,
                max_attempts,
                delay,
                type(exc).__name__,
            )
            time.sleep(delay)


def with_session_retry(
    *,
    db: Session,
    fn: Callable[[], T],
    max_attempts: int = 3,
    base_delay: float = 0.05,
    max_delay: float = 1.0,
    operation: str = "db",
) -> T:
    """Session-aware retry: rollback before each retry after a transient DB error.

    Keyword-only *db* and *fn* prevent accidental argument swaps.
    """
    return with_db_retry(
        fn,
        max_attempts=max_attempts,
        base_delay=base_delay,
        max_delay=max_delay,
        operation=operation,
        on_retry=db.rollback,
    )
