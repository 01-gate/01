"""Настоящий upstream-адаптер NanoGPT: sync + SSE, раздельные идентификаторы.

Интерфейс совпадает по имени/сути с app.upstream.stub.run_chat:
run_chat(model, messages, stream, include_usage, extra, meta) -> dict | AsyncIterator[str].

- Никаких автоматических повторов платной генерации (SPEC_004 §5.2, §6).
- Клиентский Authorization сюда не передаётся; подписывается только NANOGPT_API_KEY.
- Индентификаторы (response_id / upstream_request_id / billing_lookup_id)
  собираются в UpstreamMeta раздельно (§5.3); Request Billing не вызывается.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

logger = logging.getLogger("gateway.upstream")

# Поля публичного Chat Completions, пробрасываемые в NanoGPT. Всё остальное
# отбрасывается: умолчания upstream, инструкции (§5.2 «только нужные поля»).
FORWARD_FIELDS = (
    "stream_options",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
    "temperature",
    "top_p",
    "max_tokens",
    "max_completion_tokens",
    "stop",
    "presence_penalty",
    "frequency_penalty",
    "seed",
    "response_format",
    "logprobs",
    "top_logprobs",
)


class UpstreamError(Exception):
    """Ошибка upstream: kind in {connect, timeout, http, protocol}."""

    def __init__(self, kind: str, status: int | None = None, detail: str = ""):
        super().__init__(f"{kind}:{detail}")
        self.kind = kind
        self.status = status
        self.detail = detail


@dataclass
class UpstreamMeta:
    """Безопасные метаданные вызова для логов и будущей сверки."""

    response_id: str | None = None
    upstream_request_id: str | None = None
    billing_lookup_id: str | None = None
    usage_seen: bool = False
    upstream_status: int | None = None
    duration_ms: int = 0
    response_open_ms: int | None = None
    first_event_ms: int | None = None
    first_text_ms: int | None = None
    max_event_gap_ms: int = 0
    error: str | None = None
    error_detail: str | None = None
    events: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


def _client() -> httpx.AsyncClient:
    # фабрика, чтобы тесты подменяли transport без правок логики вызова
    timeout = float(os.environ.get("NANOGPT_TIMEOUT_SECONDS", "90"))
    return httpx.AsyncClient(
        base_url=os.environ["NANOGPT_INFERENCE_BASE_URL"].rstrip("/"),
        timeout=httpx.Timeout(timeout, connect=min(timeout, 10.0)),
    )


def _upstream_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {os.environ['NANOGPT_API_KEY']}"}


def _build_payload(
    upstream_model: str,
    messages: list[dict[str, Any]],
    stream: bool,
    include_usage: bool,
    extra: dict[str, Any],
) -> dict[str, Any]:
    payload: dict[str, Any] = {"model": upstream_model, "messages": messages}
    if stream:
        payload["stream"] = True
        if include_usage:
            payload.setdefault("stream_options", {})["include_usage"] = True
    for name in FORWARD_FIELDS:
        if name in extra:
            payload[name] = extra[name]
    return payload


def _classify(exc: Exception) -> UpstreamError:
    # ConnectTimeout — таймаут, а не ошибка соединения: 504, не 502 (SPEC_005 §4.4)
    if isinstance(exc, httpx.TimeoutException):
        return UpstreamError("timeout", detail=type(exc).__name__)
    # обрыв/невалидный протокол происходит при живом соединении — это protocol,
    # а не connect (SPEC_006 §3: не подменять диагностику предположениями)
    if isinstance(exc, httpx.RemoteProtocolError):
        return UpstreamError("protocol", detail=type(exc).__name__)
    if isinstance(exc, httpx.HTTPError):
        return UpstreamError("connect", detail=type(exc).__name__)
    return UpstreamError("protocol", detail=type(exc).__name__)


def _collect_json_ids(body: dict[str, Any], headers: httpx.Headers, meta: UpstreamMeta) -> None:
    if isinstance(body.get("id"), str):
        meta.response_id = body["id"]
    rid = headers.get("x-request-id")
    if rid:
        meta.upstream_request_id = rid
    # billing_lookup_id наблюдался как pricing.requestId и/или X-Request-ID:
    # храним явное значение pricing, а при его отсутствии — заголовочный ID.
    pricing = body.get("x_nanogpt_pricing")
    if isinstance(pricing, dict) and isinstance(pricing.get("requestId"), str):
        meta.billing_lookup_id = pricing["requestId"]
    elif rid:
        meta.billing_lookup_id = rid
    if isinstance(body.get("usage"), dict):
        meta.usage_seen = True


def _strip_pricing(body: dict[str, Any]) -> dict[str, Any]:
    # себестоимость не входит в публичный контракт (§5.4)
    if "x_nanogpt_pricing" in body:
        body = dict(body)
        body.pop("x_nanogpt_pricing", None)
    return body


async def run_chat(
    upstream_model: str,
    messages: list[dict[str, Any]],
    stream: bool,
    include_usage: bool,
    extra: dict[str, Any],
    meta: UpstreamMeta,
) -> dict[str, Any] | AsyncIterator[str]:
    payload = _build_payload(upstream_model, messages, stream, include_usage, extra)
    started = time.monotonic()
    if not stream:
        async with _client() as client:
            return await _sync(client, payload, meta, started)
    # Стадия A (SPEC_005 §4.2): upstream-запрос открыт и проверен ДО ответа
    # клиенту, поэтому 401/429/500/таймаут соединения здесь дают 502/504,
    # а не 200 с обрывом. Запрос один и тот же: второй раз не открывается (§4.3).
    client = _client()
    try:
        request = client.build_request(
            "POST", "/chat/completions", json=payload, headers=_upstream_headers()
        )
        response = await client.send(request, stream=True)
    except httpx.HTTPError as exc:
        meta.duration_ms = int((time.monotonic() - started) * 1000)
        err = _classify(exc)
        meta.error = err.kind
        await client.aclose()
        raise err from None
    except Exception as exc:
        # неожиданный сбой до ответа клиенту: типизированная 502/504 (§4.4)
        meta.duration_ms = int((time.monotonic() - started) * 1000)
        err = _classify(exc)
        meta.error = err.kind
        await client.aclose()
        raise err from None
    meta.duration_ms = int((time.monotonic() - started) * 1000)
    if response.status_code >= 400:
        await _discard_error_body(response)
        meta.upstream_status = response.status_code
        meta.error = f"http_{response.status_code}"
        await response.aclose()
        await client.aclose()
        raise UpstreamError(
            "http", status=response.status_code, detail=str(response.status_code)
        )
    meta.upstream_status = response.status_code
    meta.response_open_ms = meta.duration_ms
    rid = response.headers.get("x-request-id")
    if rid:
        meta.upstream_request_id = rid
    # Стадия B: трансляция уже открытого потока; ресурсы закрывает finally генератора
    return _stream(client, response, payload, meta, started)


async def _discard_error_body(response: httpx.Response, limit: int = 8192) -> None:
    # читается только ограниченная часть, нужная чтобы закрыть соединение;
    # тело не сохраняется и не логируется (SPEC_005 §4.2.A.4)
    got = 0
    try:
        async for chunk in response.aiter_bytes():
            got += len(chunk)
            if got >= limit:
                break
    except httpx.HTTPError:
        pass


async def _sync(
    client: httpx.AsyncClient, payload: dict[str, Any], meta: UpstreamMeta, started: float
) -> dict[str, Any]:
    try:
        resp = await client.post("/chat/completions", json=payload, headers=_upstream_headers())
    except httpx.HTTPError as exc:
        meta.duration_ms = int((time.monotonic() - started) * 1000)
        err = _classify(exc)
        meta.error = err.kind
        raise err from None
    meta.duration_ms = int((time.monotonic() - started) * 1000)
    meta.upstream_status = resp.status_code
    if resp.status_code >= 400:
        # сырое тело upstream не логируем и клиенту не отдаём (§7)
        meta.error = f"http_{resp.status_code}"
        raise UpstreamError("http", status=resp.status_code, detail=str(resp.status_code))
    try:
        body = resp.json()
    except ValueError:
        meta.error = "malformed_json"
        raise UpstreamError("protocol", status=resp.status_code, detail="not json") from None
    if not isinstance(body, dict):
        meta.error = "unexpected_json"
        raise UpstreamError("protocol", status=resp.status_code, detail="not an object")
    _collect_json_ids(body, resp.headers, meta)
    if not meta.usage_seen:
        # не значит, что запрос бесплатный; видимость для будущей сверки (§7)
        meta.extra["usage"] = "missing"
    return _strip_pricing(body)


async def _stream(
    client: httpx.AsyncClient,
    response: httpx.Response,
    payload: dict[str, Any],
    meta: UpstreamMeta,
    started: float,
) -> AsyncIterator[str]:
    """Стадия B (SPEC_005 §4.3): транслирует уже открытый upstream-ответ.

    Отдаёт SSE data-payload'ы без 'data: ' префикса; сам [DONE] не дублирует.
    Прокидывает события в исходном порядке, без буферизации. Ошибка после
    начала передачи честно обрывает поток: новый статус невозможен (§4.4).
    """
    events: AsyncIterator[str]
    last_event_at: float | None = None
    try:
        events = response.aiter_lines()
        async for line in events:
            if not line:
                continue
            if not line.startswith("data:"):
                continue  # event:/id:/retry:/комментарии — в контракт не входят
            data = line[5:].strip()
            if data == "[DONE]":
                # main добавляет единственный финальный [DONE]
                continue
            try:
                chunk = json.loads(data)
            except ValueError:
                # не-JSON data: пропускаем сырым, не разрушая поток
                meta.extra["non_json_data"] = True
                yield data
                continue
            now = time.monotonic()
            elapsed_ms = int((now - started) * 1000)
            if meta.first_event_ms is None:
                meta.first_event_ms = elapsed_ms
            if last_event_at is not None:
                gap_ms = int((now - last_event_at) * 1000)
                meta.max_event_gap_ms = max(meta.max_event_gap_ms, gap_ms)
            last_event_at = now
            if isinstance(chunk, dict):
                _collect_json_ids(chunk, response.headers, meta)
                # TTFT измеряем по первому непустому текстовому delta,
                # а не по служебному role/tool/finish_reason чанку.
                if meta.first_text_ms is None:
                    for choice in chunk.get("choices", []):
                        delta = choice.get("delta", {}) if isinstance(choice, dict) else {}
                        content = delta.get("content") if isinstance(delta, dict) else None
                        if isinstance(content, str) and content:
                            meta.first_text_ms = elapsed_ms
                            break
                rate = chunk.pop("x_nanogpt_pricing", None)
                if isinstance(rate, dict) and isinstance(rate.get("requestId"), str):
                    meta.billing_lookup_id = rate["requestId"]
                data = json.dumps(chunk, ensure_ascii=False)
            meta.events += 1
            yield data
        if not meta.usage_seen and payload.get("stream"):
            meta.extra["usage"] = "missing"
    except UpstreamError:
        raise
    except httpx.HTTPError as exc:
        # обрыв после 200: состояние потока неизвестно, повтор запрещён (§6)
        err = _classify(exc)
        meta.error = err.kind
        meta.error_detail = err.detail or type(exc).__name__
        raise err from None
    except Exception as exc:
        # неожиданный сбой после начала передачи: статус уже отправлен,
        # обрываем поток с типизированной ошибкой, а не выём горох (§4.4)
        err = _classify(exc)
        meta.error = err.kind
        meta.error_detail = err.detail or type(exc).__name__
        raise err from None
    finally:
        # длительность фиксируется при любом исходе: обрыв не должен маскироваться нулём
        meta.duration_ms = int((time.monotonic() - started) * 1000)
        await response.aclose()
        await client.aclose()