"""Reconciliation skeleton: статусы запросов и минимальная JSONL-персистентность.

SPEC_008 §5 шаг 7. Никаких денег, резервов и PostgreSQL: одна append-only
строка JSON на завершённый/оборванный chat-запрос. В файл попадают только
безопасные метаданные (id, модель, статус, usage_seen, timestamps) — никогда
не секреты и не содержимое запроса.

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
