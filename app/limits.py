"""Простые in-process лимиты chat-запросов (SPEC_008 §5 шаг 6).

Без Redis и распределённого состояния: счётчик активных запросов процесса и
optional-окно частоты на shared API key. Лимит проверяется ДО вызова upstream.
Значения читаются из env при каждом вызове, чтобы тесты могли их менять.

Функции try_acquire/release вызываются из одного event loop; критическая
секция не содержит `await`, поэтому счётчики атомарны без Lock.
"""

from __future__ import annotations

import os
import time
from collections import deque

_active = 0
_hits: deque[float] = deque()


def reset() -> None:
    """Сброс состояния процесса (используется тестами и для диагностики)."""
    global _active
    _active = 0
    _hits.clear()


def _max_concurrent() -> int:
    return max(1, int(os.environ.get("GATEWAY_MAX_CONCURRENT_CHAT", "8")))


def _rate_max() -> int:
    return max(0, int(os.environ.get("GATEWAY_RATE_LIMIT_REQUESTS", "0")))


def _rate_window() -> float:
    return max(1.0, float(os.environ.get("GATEWAY_RATE_LIMIT_WINDOW_SECONDS", "60")))


def try_acquire() -> bool:
    """Атомарно резервирует слот. False — лимит превышен, upstream не вызывать."""
    global _active
    now = time.monotonic()
    rate_max = _rate_max()
    if rate_max > 0:
        window = _rate_window()
        while _hits and now - _hits[0] > window:
            _hits.popleft()
        if len(_hits) >= rate_max:
            return False
    if _active >= _max_concurrent():
        return False
    if rate_max > 0:
        _hits.append(now)
    _active += 1
    return True


def release() -> None:
    global _active
    _active = max(0, _active - 1)
