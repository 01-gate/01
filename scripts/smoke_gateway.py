#!/usr/bin/env python3
"""Ограниченный live smoke test локального gateway: максимум 2 вызова модели.

GET /health и GET /v1/models бесплатны. Затем выполняются ровно один sync
и один SSE запрос к настроенной модели. При ошибке sync тест останавливается;
никаких автоматических повторов нет.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402


def fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


def main() -> int:
    try:
        config.load()
    except config.ConfigError as exc:
        fail(str(exc))

    if os.environ.get("GATEWAY_UPSTREAM", "stub") != "nanogpt":
        fail("GATEWAY_UPSTREAM должен быть nanogpt в проектном .env; платные запросы не отправлены")

    key = os.environ.get("GATEWAY_TEST_API_KEY", "")
    if not key:
        fail("GATEWAY_TEST_API_KEY не задан; платные запросы не отправлены")

    base_url = "http://127.0.0.1:8000"
    headers = {"Authorization": f"Bearer {key}"}
    with httpx.Client(base_url=base_url, timeout=90.0) as client:
        try:
            health = client.get("/health")
            if health.status_code != 200:
                fail(f"/health вернул HTTP {health.status_code}; платные запросы не отправлены")

            models = client.get("/v1/models")
            if models.status_code != 200:
                fail(f"/v1/models вернул HTTP {models.status_code}; платные запросы не отправлены")
            catalog = models.json()
            ids = {item.get("id") for item in catalog.get("data", [])}
            if "01" not in ids:
                fail("публичная модель 01 отсутствует в каталоге; платные запросы не отправлены")
        except httpx.HTTPError:
            fail("gateway недоступен; сначала запустите uvicorn; платные запросы не отправлены")

        print("OK: /health и публичный /v1/models")

        # Платный вызов №1: короткий синхронный ответ.
        try:
            response = client.post(
                "/v1/chat/completions",
                headers=headers,
                json={
                    "model": "01",
                    "messages": [{"role": "user", "content": "Reply with a short greeting."}],
                    "stream": False,
                    "max_tokens": 32,
                },
            )
        except httpx.HTTPError as exc:
            fail(f"sync запрос не завершился ({type(exc).__name__}); автоматического повтора нет")
        if response.status_code != 200:
            fail(f"sync запрос вернул HTTP {response.status_code}; второй платный запрос не отправлен")
        try:
            body = response.json()
            choices = body.get("choices", [])
            content = choices[0].get("message", {}).get("content") if choices else None
        except (ValueError, AttributeError, IndexError, TypeError):
            fail("sync ответ имеет неожиданный JSON; второй платный запрос не отправлен")
        if not isinstance(content, str) or not content.strip():
            fail("sync ответ не содержит текст; второй платный запрос не отправлен")
        if "x_nanogpt_pricing" in body:
            fail("в sync ответ просочилось внутреннее поле pricing")
        print("OK: реальный sync ответ через NanoGPT")

        # Платный вызов №2: короткий SSE. При неизвестном исходе не повторять автоматически.
        stream_chunks = 0
        usage_seen = False
        done_seen = False
        try:
            with client.stream(
                "POST",
                "/v1/chat/completions",
                headers=headers,
                json={
                    "model": "01",
                    "messages": [{"role": "user", "content": "Count from one to three briefly."}],
                    "stream": True,
                    "stream_options": {"include_usage": True},
                    "max_tokens": 32,
                },
            ) as response:
                if response.status_code != 200:
                    fail(f"SSE запрос вернул HTTP {response.status_code}; автоматического повтора нет")
                if not response.headers.get("content-type", "").startswith("text/event-stream"):
                    fail("SSE запрос не вернул text/event-stream; автоматического повтора нет")
                for line in response.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        done_seen = True
                        break
                    if not data:
                        continue
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        fail("SSE содержит некорректный JSON; исход генерации может быть неизвестен")
                    if "x_nanogpt_pricing" in chunk:
                        fail("в SSE просочилось внутреннее поле pricing")
                    if isinstance(chunk.get("usage"), dict):
                        usage_seen = True
                    if isinstance(chunk.get("choices"), list):
                        stream_chunks += 1
        except httpx.HTTPError as exc:
            fail(
                f"SSE оборвался ({type(exc).__name__}); исход генерации может быть неизвестен. "
                "Повтор автоматически не выполнялся."
            )

        if not done_seen:
            fail("SSE завершился без [DONE]; исход генерации может быть неизвестен. Повтор автоматически не выполнялся.")
        if stream_chunks == 0:
            fail("SSE завершился без chat chunks")
        print(f"OK: реальный SSE, chunks={stream_chunks}, [DONE]=yes, usage={'seen' if usage_seen else 'missing'}")
        print("PASS: live smoke test завершён; выполнено не более двух вызовов модели.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
