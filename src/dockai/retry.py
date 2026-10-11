"""Exponential backoff for transient failures (daemon starting up, image pulls, LLM rate limits)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")
log = logging.getLogger(__name__)


def backoff_delays(attempts: int, base: float, cap: float = 30.0) -> list[float]:
    """Delays to sleep between `attempts` tries: base, 2*base, 4*base, ... capped."""
    return [min(base * 2**i, cap) for i in range(attempts - 1)]


def retry(
    fn: Callable[[], T],
    *,
    attempts: int = 4,
    base_delay: float = 1.0,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    for delay in backoff_delays(attempts, base_delay):
        try:
            return fn()
        except retry_on as exc:
            log.warning("transient failure (%s), retrying in %.1fs", exc, delay)
            sleep(delay)
    return fn()


async def aretry(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 4,
    base_delay: float = 1.0,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
) -> T:
    for delay in backoff_delays(attempts, base_delay):
        try:
            return await fn()
        except retry_on as exc:
            log.warning("transient failure (%s), retrying in %.1fs", exc, delay)
            await asyncio.sleep(delay)
    return await fn()
