"""Reconciliation skeleton: статусы запросов и минимальная JSONL-персистентность.

SPEC_008 §5 шаг 7. Никаких денег, резервов и PostgreSQL: одна append-only
строка JSON на завершённый/оборванный chat-запрос. В файл попадают только
безопасные метаданные (id, модель, статус, нормализованные токены/cost,
длительность, счётчики сообщений) — никогда не секреты, промпты или
содержимое запроса.

shortcut: JSONL на локальном диске, без ротации и HA; при Stage C заменяется
PostgreSQL (SPEC_008 §9 A3).
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("gateway.request_log")

# Статусы жизненного цикла chat-запроса (SPEC_008 §5 шаг 7.1).
STATUSES = frozenset(
    {
        "accepted",
        "upstream_inflight",
        "succeeded",
        "upstream_error",
        "client_disconnected",
        "unknown_outcome",
        "needs_reconciliation",
    }
)


def default_log_path() -> Path:
    # путь от расположения кода, не от cwd
    return Path(__file__).resolve().parent.parent / "data" / "request_log.jsonl"


def _log_path() -> Path:
    raw = os.environ.get("GATEWAY_REQUEST_LOG_PATH", "")
    return Path(raw) if raw else default_log_path()


def record(
    *,
    gateway_request_id: str,
    model: str,
    upstream_model: str | None,
    status: str,
    usage_seen: bool,
    upstream_request_id: str | None = None,
    billing_lookup_id: str | None = None,
    error: str | None = None,
    started_at: float | None = None,
    ended_at: float | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cache_read: int | None = None,
    cache_write: int | None = None,
    reasoning: int | None = None,
    total_tokens: int | None = None,
    upstream_cost: str | None = None,
    currency: str | None = None,
    cost_seen: bool = False,
    ttft_ms: int | None = None,
    output_tps: float | None = None,
    messages_count: int | None = None,
    messages_chars: int | None = None,
    roles: str | None = None,
    cache_hit_ratio: float | None = None,
) -> None:
    if status not in STATUSES:
        status = "unknown_outcome"
    entry: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "gateway_request_id": gateway_request_id,
        "model": model,
        "upstream_model": upstream_model,
        "upstream_request_id": upstream_request_id,
        "billing_lookup_id": billing_lookup_id,
        "status": status,
        "usage_seen": bool(usage_seen),
        "error": error,
        "input": input_tokens,
        "output": output_tokens,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "reasoning": reasoning,
        "total": total_tokens,
        "upstream_cost": upstream_cost,
        "currency": currency,
        "cost_seen": bool(cost_seen),
        "duration_ms": None,
        "ttft_ms": ttft_ms,
        "output_tps": output_tps,
        "messages_count": messages_count,
        "messages_chars": messages_chars,
        "roles": roles,
        "cache_hit_ratio": cache_hit_ratio,
    }
    if started_at is not None and ended_at is not None:
        entry["duration_ms"] = int((ended_at - started_at) * 1000)
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        # персистентность не должна ронять запрос; фиксируем и продолжаем
        logger.warning("request_log write failed: %s", type(exc).__name__)
