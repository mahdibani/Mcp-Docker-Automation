"""Mask secrets before anything is printed."""

from __future__ import annotations

import re
from typing import Any

_SECRET_KEY = re.compile(r"pass(word)?|secret|token|api[_-]?key", re.IGNORECASE)
_SECRET_ASSIGNMENT = re.compile(r"((?:\w*(?:PASSWORD|SECRET|TOKEN|API_KEY)\w*)\s*=\s*)([^\s,'\"]+)", re.IGNORECASE)
MASK = "********"


def redact(value: Any) -> Any:
    """Recursively mask values whose key looks secret, and KEY=value strings like POSTGRES_PASSWORD=x."""
    if isinstance(value, dict):
        return {k: MASK if _SECRET_KEY.search(str(k)) and v else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _SECRET_ASSIGNMENT.sub(rf"\1{MASK}", value)
    return value
