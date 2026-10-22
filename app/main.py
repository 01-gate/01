"""Локальный OpenAI-compatible gateway: auth, каталог, Chat Completions."""

import hmac
import json
import logging
import os
import time
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app import config, limits, request_log
from app.models_catalog import MODEL_CATALOG
from app.upstream import nanogpt, stub

MAX_BODY_BYTES = 1_048_576

# Публичный каталог строится из единого источника правды (SPEC_008 §5 шаг 1):
# ключи MODEL_CATALOG и есть публичные ID; upstream-идентификаторы в HTTP-ответ
# не попадают. Цены и TODO сюда не входят — это не runtime-контракт этапа.
PUBLIC_MODELS = {index: {"id": model_id} for index, model_id in enumerate(MODEL_CATALOG, start=1)}

logger = logging.getLogger("gateway")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(_handler)
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())

try:
    config.load()
except config.ConfigError as exc:
    logger.critical("%s", exc)
    raise SystemExit(2) from None

app = FastAPI()


def _upstream_mode() -> str:
    # stub — по умолчанию тесты/локальная разработка; .env задаёт nanogpt для реального среза
    mode = os.environ.get("GATEWAY_UPSTREAM", "stub")
    return mode if mode in {"stub", "nanogpt"} else "stub"


def _test_key() -> str:
    return os.environ.get("GATEWAY_TEST_API_KEY", "")


if not _test_key():
    logger.warning("GATEWAY_TEST_API_KEY is not set; protected requests will be rejected with 401")


def _error(message: str, code: str, status: int, request_id: str | None = None) -> JSONResponse:
    # клиент должен уметь сопоставить любой ответ (в т.ч. ошибку) с логом
    headers = {"X-Request-ID": request_id} if request_id else None
    return JSONResponse(
        {"error": {"message": message, "type": "invalid_request_error", "code": code}},
        status_code=status,
        headers=headers,
    )


def _auth(request: Request) -> tuple[bool, bool]:
    header = request.headers.get("authorization", "")
    auth_present = bool(header)
    token = header[7:].strip() if header.startswith("Bearer ") else ""
    key = _test_key()
    auth_ok = bool(key) and bool(token) and hmac.compare_digest(token, key)
    return auth_present, auth_ok


def _auth_error(request_id: str | None = None) -> JSONResponse:
    return _error("Invalid API key", "invalid_api_key", 401, request_id)


def _upstream_error(err: nanogpt.UpstreamError, request_id: str | None = None) -> JSONResponse:
    # безопасная нормализованная ошибка без тела/заголовков upstream (§7)
    if err.kind == "timeout":
        return _error("Upstream timeout", "upstream_timeout", 504, request_id)
    if err.kind == "http":
        return _error("Upstream error", "upstream_error", 502, request_id)
    if err.kind == "protocol":
        return _error("Upstream protocol error", "upstream_protocol_error", 502, request_id)
    return _error("Upstream unavailable", "upstream_unavailable", 502, request_id)


def _message_stats(messages: list[Any]) -> tuple[int, int, str]:
    """Только счётчики/роли, без текста сообщений (SPEC_009 §5.2)."""
    counts: dict[str, int] = {}
    chars = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        role = role if isinstance(role, str) else "unknown"
        counts[role] = counts.get(role, 0) + 1
        content = message.get("content")
        if isinstance(content, str):
            chars += len(content)
    order = ("system", "user", "assistant", "tool")
    ordered = [r for r in order if r in counts]
    ordered += sorted(r for r in counts if r not in order)
    roles = ",".join(f"{r}:{counts[r]}" for r in ordered) or "-"
    return len(messages), chars, roles


def _fmt(value: Any) -> str:
    return "-" if value is None else str(value)


def _derived_metrics(
    usage: nanogpt.TokenUsage, duration_ms: int
) -> tuple[float | None, float | None]:
    output_tps = None
    if usage.output is not None and duration_ms > 0:
        output_tps = usage.output / (duration_ms / 1000)
    cache_hit_ratio = None
    if usage.cache_read is not None and usage.input is not None and usage.input > 0:
        cache_hit_ratio = usage.cache_read / usage.input
    return output_tps, cache_hit_ratio


def _log_chat_result(
    *,
    gateway_id: str,
    model: str,
    upstream_model: str,
    stream: bool,
    status: str,
    outcome: str,
    error: str | None,
    meta: nanogpt.UpstreamMeta,
    messages_count: int,
    messages_chars: int,
    roles: str,
    extra_fields: list[str],
) -> None:
    """Две INFO-строки (summary + tokens) и полный диагностический DEBUG."""
    usage = meta.token_usage
    ttft_ms = meta.first_text_ms if meta.first_text_ms is not None else meta.first_event_ms
    duration_ms = meta.duration_ms
    logger.info(
        "chat gateway_id=%s upstream_id=%s upstream_request_id=%s billing_lookup_id=%s"
        " model=%s upstream_model=%s stream=%d status=%s outcome=%s duration_ms=%s"
        " ttft_ms=%s upstream_status=%s error=%s",
        gateway_id,
        _fmt(meta.response_id),
        _fmt(meta.upstream_request_id),
        _fmt(meta.billing_lookup_id),
        model,
        upstream_model,
        int(stream),
        status,
        outcome,
        duration_ms,
        _fmt(ttft_ms),
        _fmt(meta.upstream_status),
        error or "-",
    )

    output_tps, cache_hit_ratio = _derived_metrics(usage, duration_ms)
    cost = usage.upstream_cost if usage.cost_seen else "unknown"
    currency = usage.currency or "-"
    tps = f"{output_tps:.1f}" if output_tps is not None else "-"
    hit = f"{cache_hit_ratio:.3f}" if cache_hit_ratio is not None else "-"

    if meta.usage_seen:
        logger.info(
            "tokens gateway_id=%s in=%s out=%s cache_r=%s cache_w=%s reason=%s"
            " total=%s cost=%s currency=%s cost_seen=%d usage_seen=1 tps=%s"
            " cache_hit=%s msg=%s chars=%s roles=%s",
            gateway_id,
            _fmt(usage.input),
            _fmt(usage.output),
            _fmt(usage.cache_read),
            _fmt(usage.cache_write),
            _fmt(usage.reasoning),
            _fmt(usage.total),
            cost,
            currency,
            int(usage.cost_seen),
            tps,
            hit,
            messages_count,
            messages_chars,
            roles,
        )
    else:
        # отсутствие usage не означает бесплатный запрос (SPEC_009 §5.3)
        logger.info(
            "tokens gateway_id=%s usage=missing cost=%s currency=%s cost_seen=%d"
            " usage_seen=0 msg=%s chars=%s roles=%s",
            gateway_id,
            cost,
            currency,
            int(usage.cost_seen),
            messages_count,
            messages_chars,
            roles,
        )

    logger.debug(
        "chat_debug gateway_id=%s model=%s events=%s response_open_ms=%s"
        " first_event_ms=%s first_text_ms=%s first_tool_call_ms=%s"
        " tool_call_events=%s tool_call_fragments=%s first_reasoning_ms=%s"
        " reasoning_events=%s max_event_gap_ms=%s sse_event_types=%s"
        " delta_keys=%s finish_reasons=%s upstream_fields=%s extra_fields=%s"
        " error_detail=%s error=%s",
        gateway_id,
        model,
        meta.events,
        _fmt(meta.response_open_ms),
        _fmt(meta.first_event_ms),
        _fmt(meta.first_text_ms),
        _fmt(meta.first_tool_call_ms),
        meta.tool_call_events,
        meta.tool_call_fragments,
        _fmt(meta.first_reasoning_ms),
        meta.reasoning_events,
        meta.max_event_gap_ms,
        ",".join(f"{k}:{v}" for k, v in sorted(meta.sse_event_types.items())) or "-",
        ",".join(f"{k}:{v}" for k, v in sorted(meta.delta_key_counts.items())) or "-",
        ",".join(f"{k}:{v}" for k, v in sorted(meta.finish_reason_counts.items())) or "-",
        meta.extra.get("upstream_fields", "-"),
        ",".join(extra_fields) or "-",
        meta.error_detail or "-",
        error or "-",
    )


@app.get("/health")
async def health(request: Request):
    # Keep the public health contract minimal. A valid gateway key additionally
    # gets the configured upstream mode so diagnostics can refuse stub-mode runs.
    body = {"status": "ok"}
    _, auth_ok = _auth(request)
    if auth_ok:
        body["upstream_mode"] = _upstream_mode()
    return JSONResponse(body)


@app.get("/v1/models")
async def list_models():
    # публичный endpoint: авторизации нет по решению (§2.4, §4.1)
    return JSONResponse({"object": "list", "data": list(PUBLIC_MODELS.values())})


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    gateway_request_id = str(uuid.uuid4())
    started = time.monotonic()

    def log_chat(extra: str, *args: Any) -> None:
        # единый structured log chat path: correlation id во всех строках
        logger.info(
            "chat method=POST path=/v1/chat/completions gateway_request_id=%s " + extra,
            gateway_request_id,
            *args,
        )

    auth_present, auth_ok = _auth(request)
    if not auth_ok:
        log_chat(
            "status=401 auth_present=%s auth_ok=False key_set=%s",
            auth_present,
            bool(_test_key()),
        )
        return _auth_error(gateway_request_id)

    body = bytearray()
    async for part in request.stream():
        body.extend(part)
        if len(body) > MAX_BODY_BYTES:
            log_chat("status=413 auth_present=%s auth_ok=True", auth_present)
            return _error("Request body too large", "body_too_large", 413, gateway_request_id)

    try:
        payload = json.loads(body)
    except ValueError:
        log_chat("status=400 auth_present=%s auth_ok=True", auth_present)
        return _error("Invalid JSON body", "invalid_json", 400, gateway_request_id)
    if not isinstance(payload, dict):
        log_chat("status=400 auth_present=%s auth_ok=True", auth_present)
        return _error("Invalid JSON body", "invalid_json", 400, gateway_request_id)

    model = payload.get("model")
    messages = payload.get("messages")
    stream = payload.get("stream", False)
    stream_options = payload.get("stream_options", {})

    def bad_request(code: str, message: str) -> JSONResponse:
        log_chat(
            "status=400 auth_present=%s auth_ok=True model=%s stream=%s",
            auth_present,
            model if isinstance(model, str) else "-",
            stream if isinstance(stream, bool) else "-",
        )
        return _error(message, code, 400, gateway_request_id)

    if not isinstance(model, str) or not model:
        return bad_request("invalid_model", "Invalid `model` field")
    if not isinstance(messages, list) or not messages:
        return bad_request("invalid_messages", "Invalid `messages` field")
    if not all(isinstance(m, dict) and isinstance(m.get("role"), str) for m in messages):
        return bad_request("invalid_messages", "Invalid `messages` field")
    if not isinstance(stream, bool):
        return bad_request("invalid_request_error", "Invalid `stream` field")
    if not isinstance(stream_options, dict):
        return bad_request("invalid_request_error", "Invalid `stream_options` field")

    if model not in MODEL_CATALOG:
        # каталог и accept-list обязаны совпадать; atomic-проверка до вызова upstream
        log_chat(
            "status=404 auth_present=%s auth_ok=True model=%s stream=%s upstream_call=False",
            auth_present,
            model,
            stream,
        )
        return _error(f"The model `{model}` does not exist", "model_not_found", 404, gateway_request_id)

    include_usage = bool(stream_options.get("include_usage", False))
    extra_fields = sorted(set(payload) - {"model", "messages", "stream", "stream_options"})
    extra = {k: payload[k] for k in extra_fields}
    upstream_model = MODEL_CATALOG[model]

    # basic лимиты проверяются ДО вызова upstream (SPEC_008 §5 шаг 6)
    if not limits.try_acquire():
        log_chat(
            "status=429 auth_present=%s auth_ok=True model=%s stream=%s upstream_call=False",
            auth_present,
            model,
            stream,
        )
        return _error("Rate limit exceeded", "rate_limit_exceeded", 429, gateway_request_id)

    try:
        response = await _run_chat(
            request,
            gateway_request_id,
            started,
            auth_present,
            model,
            upstream_model,
            messages,
            stream,
            include_usage,
            extra,
            extra_fields,
        )
    except Exception:
        limits.release()
        raise
    if isinstance(response, StreamingResponse):
        # слот удерживается до конца стрима: генератор освобождает его сам
        return response
    limits.release()
    return response


async def _run_chat(
    request: Request,
    gateway_request_id: str,
    started: float,
    auth_present: bool,
    model: str,
    upstream_model: str,
    messages: list[Any],
    stream: bool,
    include_usage: bool,
    extra: dict[str, Any],
    extra_fields: list[str],
) -> Any:
    messages_count, messages_chars, roles = _message_stats(messages)
    is_nanogpt = _upstream_mode() == "nanogpt"
    public_upstream_model = upstream_model if is_nanogpt else "-"

    if is_nanogpt:
        meta = nanogpt.UpstreamMeta()

        def log_result(status: str, outcome: str, error: str | None) -> None:
            _log_chat_result(
                gateway_id=gateway_request_id,
                model=model,
                upstream_model=public_upstream_model,
                stream=stream,
                status=status,
                outcome=outcome,
                error=error,
                meta=meta,
                messages_count=messages_count,
                messages_chars=messages_chars,
                roles=roles,
                extra_fields=extra_fields,
            )

        def record(status: str, error: str | None) -> None:
            usage = meta.token_usage
            output_tps, cache_hit_ratio = _derived_metrics(usage, meta.duration_ms)
            request_log.record(
                gateway_request_id=gateway_request_id,
                model=model,
                upstream_model=upstream_model,
                status=status,
                usage_seen=meta.usage_seen,
                upstream_request_id=meta.upstream_request_id,
                billing_lookup_id=meta.billing_lookup_id,
                error=error,
                started_at=started,
                ended_at=time.monotonic(),
                input_tokens=usage.input,
                output_tokens=usage.output,
                cache_read=usage.cache_read,
                cache_write=usage.cache_write,
                reasoning=usage.reasoning,
                total_tokens=usage.total,
                upstream_cost=usage.upstream_cost,
                currency=usage.currency,
                cost_seen=usage.cost_seen,
                ttft_ms=(
                    meta.first_text_ms
                    if meta.first_text_ms is not None
                    else meta.first_event_ms
                ),
                output_tps=output_tps,
                messages_count=messages_count,
                messages_chars=messages_chars,
                roles=roles,
                cache_hit_ratio=cache_hit_ratio,
            )

        try:
            # заявка на сверку живёт ДО ожидания upstream: если процесс упадёт
            # во время генерации, запись с gateway_request_id уже на диске
            record("upstream_inflight", None)
            result = await nanogpt.run_chat(upstream_model, messages, stream, include_usage, extra, meta)
        except nanogpt.UpstreamError as err:
            log_result("504" if err.kind == "timeout" else "502", "upstream_error", err.kind)
            record("upstream_error", err.kind)
            return _upstream_error(err, gateway_request_id)

        if not stream:
            log_result("200", "succeeded", None)
            record("succeeded", None)
            headers = {"X-Request-ID": gateway_request_id}
            if meta.upstream_request_id:
                headers["X-Upstream-Request-ID"] = meta.upstream_request_id
            return JSONResponse(result, headers=headers)

        # upstream уже ответил: id известны, фиксируем их в заявке на сверку
        # до того, как стрим начнёт отдавать данные клиенту
        record("upstream_inflight", None)

        async def sse_nano() -> Any:
            error: str | None = None
            done = False
            try:
                async for data in result:  # type: ignore[union-attr]
                    yield f"data: {data}\n\n"
                yield "data: [DONE]\n\n"
                done = True
            except nanogpt.UpstreamError as err:
                # статус уже отправлен: обрываем поток честно, без повторов (§6, §7)
                error = err.kind
            except Exception as exc:
                # неожиданный сбой после отправки статуса: фиксируем в логе,
                # а не выдаём за штатное завершение (SPEC_006 §2)
                error = type(exc).__name__
            finally:
                if done:
                    outcome = "succeeded"
                    record_status = "succeeded"
                elif error is None:
                    # генератор закрыт до [DONE] без ошибки: disconnect клиента,
                    # исход платной генерации неизвестен (SPEC_008 §5 шаг 7.4)
                    outcome = "client_disconnected"
                    record_status = "client_disconnected"
                elif meta.upstream_request_id:
                    # ошибка после старта, но upstream id известен — нужна сверка
                    outcome = "needs_reconciliation"
                    record_status = "needs_reconciliation"
                else:
                    outcome = "unknown_outcome"
                    record_status = "unknown_outcome"
                log_result("200", outcome, error)
                record(record_status, error)
                limits.release()

        response_headers = {
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Request-ID": gateway_request_id,
        }
        if meta.upstream_request_id:
            response_headers["X-Upstream-Request-ID"] = meta.upstream_request_id
        return StreamingResponse(sse_nano(), media_type="text/event-stream", headers=response_headers)

    # legacy stub-режим: прежний интерфейс и поведение (§2.11)
    meta = nanogpt.UpstreamMeta()
    result = await stub.run_chat(model, messages, stream, include_usage, extra)

    def log_stub(status: str, outcome: str, error: str | None) -> None:
        _log_chat_result(
            gateway_id=gateway_request_id,
            model=model,
            upstream_model=public_upstream_model,
            stream=stream,
            status=status,
            outcome=outcome,
            error=error,
            meta=meta,
            messages_count=messages_count,
            messages_chars=messages_chars,
            roles=roles,
            extra_fields=extra_fields,
        )

    def record_stub(status: str) -> None:
        usage = meta.token_usage
        output_tps, cache_hit_ratio = _derived_metrics(usage, meta.duration_ms)
        request_log.record(
            gateway_request_id=gateway_request_id,
            model=model,
            upstream_model=None,
            status=status,
            usage_seen=meta.usage_seen,
            started_at=started,
            ended_at=time.monotonic(),
            input_tokens=usage.input,
            output_tokens=usage.output,
            cache_read=usage.cache_read,
            cache_write=usage.cache_write,
            reasoning=usage.reasoning,
            total_tokens=usage.total,
            upstream_cost=usage.upstream_cost,
            currency=usage.currency,
            cost_seen=usage.cost_seen,
            ttft_ms=(
                meta.first_text_ms if meta.first_text_ms is not None else meta.first_event_ms
            ),
            output_tps=output_tps,
            messages_count=messages_count,
            messages_chars=messages_chars,
            roles=roles,
            cache_hit_ratio=cache_hit_ratio,
        )

    if not stream:
        meta.response_id = result.get("id") if isinstance(result, dict) else None
        meta.duration_ms = int((time.monotonic() - started) * 1000)
        meta.token_usage = nanogpt.extract_token_usage(result)
        meta.usage_seen = meta.token_usage.usage_seen
        log_stub("200", "succeeded", None)
        record_stub("succeeded")
        return JSONResponse(result, headers={"X-Request-ID": gateway_request_id})

    async def sse() -> Any:
        done = False
        try:
            async for chunk in result:  # type: ignore[union-attr]
                elapsed_ms = int((time.monotonic() - started) * 1000)
                if meta.first_event_ms is None:
                    meta.first_event_ms = elapsed_ms
                if isinstance(chunk, dict):
                    if isinstance(chunk.get("id"), str):
                        meta.response_id = chunk["id"]
                    content = ""
                    for choice in chunk.get("choices", []):
                        if isinstance(choice, dict):
                            delta = choice.get("delta", {})
                            if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                                content += delta["content"]
                    if content and meta.first_text_ms is None:
                        meta.first_text_ms = elapsed_ms
                    _meta_usage = nanogpt.extract_token_usage(chunk)
                    nanogpt.merge_token_usage(meta.token_usage, _meta_usage)
                    meta.usage_seen = meta.usage_seen or _meta_usage.usage_seen
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
            done = True
        finally:
            meta.duration_ms = int((time.monotonic() - started) * 1000)
            # лог здесь: request_id известен и при disconnect клиента
            log_stub("200", "succeeded" if done else "client_disconnected", None)
            record_stub("succeeded" if done else "client_disconnected")
            limits.release()

    return StreamingResponse(
        sse(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Request-ID": gateway_request_id,
        },
    )
