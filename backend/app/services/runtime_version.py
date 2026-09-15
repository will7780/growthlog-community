"""Sanitized semantic version exposed to the product UI."""
from __future__ import annotations

import re

_PUBLIC_SEMVER_RE = re.compile(
    r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z.-]+)?$"
)
DEVELOPMENT_VERSION = "0.0.0-dev"


def sanitize_public_version(value: object) -> str:
    candidate = str(value or "").strip()
    if _PUBLIC_SEMVER_RE.fullmatch(candidate):
        return candidate
    return DEVELOPMENT_VERSION
