"""Сбор данных моделей NanoGPT.

Пишет:
- models.json      — прежний детальный дамп по upstream-id (не ломаем)
- pi.models.json   — конфиг для агента Pi (публичные id + name из каталога)
"""

import json
import sys
import os
import urllib.request
from pathlib import Path
from app import config

# Запуск и как `python extract_models.py`, и как `python -m extract_models`.
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    config.load()
except config.ConfigError as exc:
    raise SystemExit(2) from None

from app.models_catalog import MODEL_CATALOG

URL = "https://nano-gpt.com/api/v1/models?detailed=true"

FIELDS = [
    "reasoning_efforts",
    "pricing",
    "cost_estimate",
    "context_length",
    "max_output_tokens",
]

PI_LEVELS = ["off", "minimal", "low", "medium", "high", "xhigh", "max"]


def extract_items(payload):
    """Ответ может быть списком, либо объектом с data/models/items."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "models", "items", "results"):
            if isinstance(payload.get(key), list):
                return payload[key]
        if all(isinstance(v, dict) for v in payload.values()):
            return [{"id": k, **v} for k, v in payload.items()]
    raise ValueError("Не удалось найти список моделей в ответе")


def get_model_id(item):
    if "id" in item:
        return item["id"]
    if isinstance(item.get("model"), dict) and "id" in item["model"]:
        return item["model"]["id"]
    if isinstance(item.get("model"), str):
        return item["model"]
    return None


def _per_1m(value):
    if value is None:
        return None
    try:
        return float(value) * 1000
    except (TypeError, ValueError):
        return None


def normalize_pricing(raw: dict | None) -> dict | None:
    if not isinstance(raw, dict):
        return None
    return {
        "input": raw.get("prompt"),
        "output": raw.get("completion"),
        "cacheRead": _per_1m(raw.get("cacheReadInputPer1kTokens")),
        "cacheWrite": _per_1m(raw.get("cacheWriteInputPer1kTokens")) or 0,
    }


def build_thinking_level_map(efforts: list | None) -> dict:
    """Фиксированные ключи Pi.
    Значение = уровень, если он есть в reasoning_efforts, иначе null.
    'none' из NanoGPT мапится на 'off'.
    """
    efforts = efforts or []
    normalized = set()
    for e in efforts:
        if e == "none":
            normalized.add("off")
        else:
            normalized.add(e)

    result = {}
    for level in PI_LEVELS:
        if level == "off":
            result["off"] = "off"
        else:
            result[level] = level if level in normalized else None
    return result


def main():
    req = urllib.request.Request(
        URL,
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    items = extract_items(payload)
    by_upstream: dict[str, dict] = {}
    for item in items:
        mid = get_model_id(item)
        if mid:
            src = item
            if all(f not in item for f in FIELDS) and isinstance(item.get("model"), dict):
                src = item["model"]
            by_upstream[mid] = src

    # --- models.json: детальный дамп по upstream ---
    old_result = []
    for entry in MODEL_CATALOG:
        upstream = entry["url"]
        src = by_upstream.get(upstream)
        if src is None:
            old_result.append({"id": upstream, **{f: None for f in FIELDS}})
            continue
        old_result.append({
            "id": upstream,
            **{f: src.get(f) for f in FIELDS},
        })
        old_result["context_length"] = 250000
        old_result["max_output_tokens"] = 12500

    # Пишем в текущую рабочую директорию (откуда запустили скрипт)
    models_path = Path("models.json")
    with models_path.open("w", encoding="utf-8") as f:
        json.dump(old_result, f, ensure_ascii=False, indent=2)
    print(f"models.json: {len(old_result)} записей → {models_path.resolve()}")

    # --- pi.models.json ---
    pi_models = []
    for entry in MODEL_CATALOG:
        upstream = entry["url"]
        src = by_upstream.get(upstream) or {}
        pricing = normalize_pricing(src.get("pricing"))
        efforts = src.get("reasoning_efforts") or []

        idx = MODEL_CATALOG.index(entry) + 1
        numbered_name = f"{idx:02d} {entry['name']}"

        pi_models.append({
            "id": entry["id"],
            "name": numbered_name,
            "reasoning": bool(efforts),
            "contextWindow": 150000, #src.get("context_length"),
            "maxTokens": 10000, #src.get("max_output_tokens"),
            "cost": pricing or {
                "input": None,
                "output": None,
                "cacheRead": None,
                "cacheWrite": 0,
            },
            "thinkingLevelMap": build_thinking_level_map(efforts),
        })

    pi_payload = {
        "$schema": "https://pi.dev/schemas/models.schema.json",
        "providers": {
            "01r": {
                "baseUrl": "https://0.1ncript0.ru/v1/",
                "api": "openai-completions",
                "apiKey": os.environ.get("GATEWAY_TEST_API_KEY", ""),
                "models": pi_models,
            }
        },
    }

    pi_path = Path("../../.pi/agent/models.json")
    with pi_path.open("w", encoding="utf-8") as f:
        json.dump(pi_payload, f, ensure_ascii=False, indent=2)
    print(f"pi.models.json: {len(pi_models)} моделей → {pi_path.resolve()}")


if __name__ == "__main__":
    main()
