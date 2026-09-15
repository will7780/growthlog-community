"""Validate Web Push subscription endpoints (SSRF-safe host allowlist)."""
from __future__ import annotations

import ipaddress
import re
from typing import Iterable, Optional
from urllib.parse import urlparse

from app.config import settings

# Fixed host used only when WEB_PUSH_USE_FAKE=true.
FAKE_PUSH_HOST = "push.example.test"

# Verified browser push providers (suffix allowlist). Configurable via env.
DEFAULT_PUSH_HOST_SUFFIXES = (
    "web.push.apple.com",
    "push.apple.com",
    "fcm.googleapis.com",
    "android.googleapis.com",
    "updates.push.services.mozilla.com",
    "push.services.mozilla.com",
)

_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def push_host_suffixes() -> tuple[str, ...]:
    raw = (getattr(settings, "web_push_allowed_host_suffixes", None) or "").strip()
    if raw:
        items = tuple(s.strip().lower().lstrip(".") for s in raw.split(",") if s.strip())
        return items or DEFAULT_PUSH_HOST_SUFFIXES
    return DEFAULT_PUSH_HOST_SUFFIXES


def host_matches_suffix(host: str, suffix: str) -> bool:
    """Exact match or subdomain under suffix. Rejects suffix-tail forgery like evilpush.apple.com."""
    h = (host or "").lower().rstrip(".")
    s = (suffix or "").lower().lstrip(".").rstrip(".")
    if not h or not s:
        return False
    if h == s:
        return True
    return h.endswith("." + s)


def _is_ip_literal(host: str) -> bool:
    text = host.strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        return False


def _is_disallowed_hostname(host: str) -> bool:
    h = host.lower().rstrip(".")
    if not h:
        return True
    if h == "localhost" or h.endswith(".localhost"):
        return True
    if _is_ip_literal(h):
        return True
    return False


def validate_push_endpoint(endpoint: str, *, allow_fake_host: Optional[bool] = None) -> str:
    """
    Return normalized endpoint or raise ValueError with safe error code (never include endpoint).
    """
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise ValueError("INVALID_ENDPOINT")
    endpoint = endpoint.strip()
    if len(endpoint) > 2048:
        raise ValueError("ENDPOINT_TOO_LONG")
    if _CTRL.search(endpoint) or "\\" in endpoint:
        raise ValueError("INVALID_ENDPOINT")

    parsed = urlparse(endpoint)
    if parsed.scheme != "https":
        raise ValueError("ENDPOINT_MUST_HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("ENDPOINT_CREDENTIALS_FORBIDDEN")
    try:
        host = (parsed.hostname or "").lower()
    except ValueError:
        # Malformed netloc / port fragments must not leak parser text.
        raise ValueError("ENDPOINT_PORT_FORBIDDEN") from None
    if not host:
        raise ValueError("INVALID_ENDPOINT")
    if _is_disallowed_hostname(host):
        raise ValueError("ENDPOINT_HOST_FORBIDDEN")

    try:
        port = parsed.port
    except ValueError:
        # Non-numeric or out-of-range ports (e.g. :abc, :99999).
        raise ValueError("ENDPOINT_PORT_FORBIDDEN") from None
    if port is not None and port != 443:
        raise ValueError("ENDPOINT_PORT_FORBIDDEN")

    use_fake = (
        bool(settings.web_push_use_fake)
        if allow_fake_host is None
        else bool(allow_fake_host)
    )
    if use_fake:
        # Fake mode: only the fixed isolate/test host (exact match).
        if host != FAKE_PUSH_HOST:
            raise ValueError("ENDPOINT_HOST_FORBIDDEN")
        return endpoint

    allowed = push_host_suffixes()
    if not any(host_matches_suffix(host, suffix) for suffix in allowed):
        raise ValueError("ENDPOINT_HOST_FORBIDDEN")
    return endpoint


def assert_hosts_allowed(hosts: Iterable[str]) -> None:
    """Test helper."""
    for host in hosts:
        validate_push_endpoint(f"https://{host}/v1/x")
