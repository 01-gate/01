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
from decimal import Decimal, InvalidOperation
from typing import Any, AsyncIterator

import httpx

logger = logging.getLogger("gateway.upstream")

MAX_TOKENS=10000

# Поля публичного Chat Completions, пробрасываемые в NanoGPT. Всё остальное
# отбрасывается: умолчания upstream, инструкции (§5.2 «только нужные поля»).
FORWARD_FIELDS = (
    "stream_options",
    "reasoning_effort",
    "reasoning",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
    "temperature",
    "top_p",
    "stop",
    "presence_penalty",
    "frequency_penalty",
    "seed",
    "response_format",
    "logprobs",
    "top_logprobs",
)

MAX_FIELDS = ("max_tokens", "max_completion_tokens", "max_output_tokens")


class UpstreamError(Exception):
    """Ошибка upstream: kind in {connect, timeout, http, protocol}."""

    def __init__(self, kind: str, status: int | None = None, detail: str = ""):
        super().__init__(f"{kind}:{detail}")
        self.kind = kind
        self.status = status
        self.detail = detail


@dataclass
class TokenUsage:
    """Нормализованный наблюдаемый usage/cost upstream (SPEC_009 §5.1).

    cost — decimal-строка только для лога/JSONL, не источник финансовой истины.
    """

    input: int | None = None
    output: int | None = None
    cache_read: int | None = None
    cache_write: int | None = None
    reasoning: int | None = None
    total: int | None = None
    upstream_cost: str | None = None
    currency: str | None = None
    usage_seen: bool = False
    cost_seen: bool = False
    # приоритет источника cost: 0 нет, 1 usdCost, 2 amount, 3 usage.cost
    cost_rank: int = 0


@dataclass
class UpstreamMeta:
    """Безопасные метаданные вызова для логов и будущей сверки."""

    response_id: str | None = None
    upstream_request_id: str | None = None
    billing_lookup_id: str | None = None
    usage_seen: bool = False
    token_usage: TokenUsage = field(default_factory=TokenUsage)
    upstream_status: int | None = None
    duration_ms: int = 0
    response_open_ms: int | None = None
    first_event_ms: int | None = None
    first_text_ms: int | None = None
    first_tool_call_ms: int | None = None
    first_reasoning_ms: int | None = None
    max_event_gap_ms: int = 0
    tool_call_events: int = 0
    tool_call_fragments: int = 0
    reasoning_events: int = 0
    delta_key_counts: dict[str, int] = field(default_factory=dict)
    sse_event_types: dict[str, int] = field(default_factory=dict)
    finish_reason_counts: dict[str, int] = field(default_factory=dict)
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
    # NanoGPT документирует Authorization: Bearer <inference key>.
    # Клиентский Authorization gateway сюда не попадает (SPEC_004 §5.1).
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

    for key in MAX_FIELDS:
        if key in extra:
            value = extra[key]
            payload[key] = value if value < MAX_TOKENS else MAX_TOKENS

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


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _first_int(*values: Any) -> int | None:
    for value in values:
        parsed = _as_int(value)
        if parsed is not None:
            return parsed
    return None


def _as_cost(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        parsed = Decimal(str(value))
    except InvalidOperation:
        return None
    if not parsed.is_finite():
        return None
    # decimal-строка без float-потери; для лога и JSONL, не ledger
    return format(parsed, "f")


def _as_currency(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()[:16]
    return None


def extract_token_usage(body: dict[str, Any]) -> TokenUsage:
    """Маппит известные ключи usage/pricing в нормализованную структуру.

    Приоритет cost зафиксирован: usage.cost → pricing.amount → pricing.costUsd/
    usdCost. Отсутствующие значения остаются None; счётчики не суммируются
    вслепую (reasoning может быть подмножеством output).
    """
    usage = body.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    pricing = body.get("x_nanogpt_pricing")
    pricing = pricing if isinstance(pricing, dict) else {}
    prompt_details = usage.get("prompt_tokens_details")
    prompt_details = prompt_details if isinstance(prompt_details, dict) else {}
    completion_details = usage.get("completion_tokens_details")
    completion_details = completion_details if isinstance(completion_details, dict) else {}

    out = TokenUsage(usage_seen=isinstance(body.get("usage"), dict))
    out.input = _first_int(usage.get("prompt_tokens"), pricing.get("inputTokens"))
    out.output = _first_int(usage.get("completion_tokens"), pricing.get("outputTokens"))
    out.cache_read = _first_int(
        prompt_details.get("cached_tokens"),
        usage.get("cached_tokens"),
        pricing.get("cacheReadInputTokens"),
    )
    out.cache_write = _first_int(
        usage.get("cache_write_tokens"),
        prompt_details.get("cache_write_tokens"),
        pricing.get("cacheWriteInputTokens"),
    )
    out.reasoning = _first_int(
        usage.get("reasoning_tokens"),
        completion_details.get("reasoning_tokens"),
        prompt_details.get("reasoning_tokens"),
        pricing.get("reasoningTokens"),
    )
    out.total = _first_int(usage.get("total_tokens"))
    if out.total is None and out.input is not None and out.output is not None:
        out.total = out.input + out.output

    cost = _as_cost(usage.get("cost"))
    if cost is not None:
        out.upstream_cost = cost
        out.currency = _as_currency(usage.get("currency")) or _as_currency(pricing.get("currency"))
        out.cost_rank = 3
    else:
        cost = _as_cost(pricing.get("amount"))
        if cost is not None:
            out.upstream_cost = cost
            out.currency = _as_currency(pricing.get("currency"))
            out.cost_rank = 2
        else:
            cost = _as_cost(pricing.get("costUsd"))
            if cost is None:
                cost = _as_cost(pricing.get("usdCost"))
            if cost is not None:
                out.upstream_cost = cost
                out.currency = "USD"
                out.cost_rank = 1
    out.cost_seen = out.upstream_cost is not None
    return out


def merge_token_usage(target: TokenUsage, parsed: TokenUsage) -> None:
    # stream приходит несколькими чанками: не затирать уже известные поля
    for name in ("input", "output", "cache_read", "cache_write", "reasoning", "total"):
        if getattr(target, name) is None:
            setattr(target, name, getattr(parsed, name))
    if parsed.cost_rank > target.cost_rank:
        target.upstream_cost = parsed.upstream_cost
        target.currency = parsed.currency
        target.cost_rank = parsed.cost_rank
    target.usage_seen = target.usage_seen or parsed.usage_seen
    target.cost_seen = target.upstream_cost is not None


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
    merge_token_usage(meta.token_usage, extract_token_usage(body))


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
    # Record only field names, never values, to verify the actual upstream contract.
    meta.extra["upstream_fields"] = ",".join(sorted(payload))
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
    current_sse_event = "message"
    try:
        events = response.aiter_lines()
        async for line in events:
            if not line:
                current_sse_event = "message"
                continue
            if line.startswith(":"):
                continue  # SSE-комментарий keep-alive
            if line.startswith("event:"):
                candidate = line[6:].strip()
                # Do not let arbitrary upstream strings become log fields.
                safe_type = "".join(c for c in candidate[:40] if c.isalnum() or c in "_-.")
                current_sse_event = safe_type or "message"
                continue
            if line.startswith("id:") or line.startswith("retry:"):
                continue
            if not line.startswith("data:"):
                continue
            event_type = current_sse_event
            data = line[5:].strip()
            if data == "[DONE]":
                # main добавляет единственный финальный [DONE]
                continue
            meta.sse_event_types[event_type] = meta.sse_event_types.get(event_type, 0) + 1
            current_sse_event = "message"
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
                # Metrics inspect only delta shape and field names, never content.
                for choice in chunk.get("choices", []):
                    if not isinstance(choice, dict):
                        continue
                    finish_reason = choice.get("finish_reason")
                    if isinstance(finish_reason, str):
                        # Log only recognized protocol labels; arbitrary provider text is not a log field.
                        safe_finish_reason = finish_reason if finish_reason in {
                            "stop", "length", "tool_calls", "function_call", "content_filter"
                        } else "other"
                        meta.finish_reason_counts[safe_finish_reason] = (
                            meta.finish_reason_counts.get(safe_finish_reason, 0) + 1
                        )
                    delta = choice.get("delta", {})
                    if not isinstance(delta, dict):
                        continue
                    for key in delta:
                        # Delta keys are metadata too: bound and sanitize them before logging.
                        safe_key = "".join(
                            c for c in str(key)[:40] if c.isalnum() or c in "_-."
                        ) or "other"
                        meta.delta_key_counts[safe_key] = meta.delta_key_counts.get(safe_key, 0) + 1
                    content = delta.get("content")
                    if meta.first_text_ms is None and isinstance(content, str) and content:
                        meta.first_text_ms = elapsed_ms
                    tool_calls = delta.get("tool_calls")
                    if isinstance(tool_calls, list) and tool_calls:
                        meta.tool_call_events += 1
                        meta.tool_call_fragments += len(tool_calls)
                        if meta.first_tool_call_ms is None:
                            meta.first_tool_call_ms = elapsed_ms
                    reasoning_keys = {"reasoning", "reasoning_content", "analysis", "thinking"}
                    if any(key in delta and delta[key] not in (None, "", [], {}) for key in reasoning_keys):
                        meta.reasoning_events += 1
                        if meta.first_reasoning_ms is None:
                            meta.first_reasoning_ms = elapsed_ms
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