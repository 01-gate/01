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
            log_chat,
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
    log_chat: Any,
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
    base_log = (
        "log_extra method=POST path=/v1/chat/completions auth_present=%s auth_ok=True model=%s "
        "upstream_model=%s stream=%s messages_count=%s extra_fields=%s"
    )
    base_args = (
        auth_present,
        model,
        upstream_model if _upstream_mode() == "nanogpt" else "-",
        stream,
        len(messages),
        ",".join(extra_fields) or "-",
    )

    nano_log = (
        base_log
        + " status=%s response_id=%s upstream_request_id=%s billing_lookup_id=%s"
        " upstream_status=%s usage=%s events=%s duration_ms=%s"
        " response_open_ms=%s first_event_ms=%s first_text_ms=%s"
        " first_tool_call_ms=%s tool_call_events=%s tool_call_fragments=%s"
        " first_reasoning_ms=%s reasoning_events=%s max_event_gap_ms=%s"
        " sse_event_types=%s delta_keys=%s finish_reasons=%s upstream_fields=%s"
        " error_detail=%s error=%s"
    )

    if _upstream_mode() == "nanogpt":
        meta = nanogpt.UpstreamMeta()

        def meta_args(status: str, error: str) -> tuple[Any, ...]:
            return (
                status,
                meta.response_id or "-",
                meta.upstream_request_id or "-",
                meta.billing_lookup_id or "-",
                meta.upstream_status if meta.upstream_status is not None else "-",
                "seen" if meta.usage_seen else meta.extra.get("usage", "-"),
                meta.events,
                meta.duration_ms,
                meta.response_open_ms if meta.response_open_ms is not None else "-",
                meta.first_event_ms if meta.first_event_ms is not None else "-",
                meta.first_text_ms if meta.first_text_ms is not None else "-",
                meta.first_tool_call_ms if meta.first_tool_call_ms is not None else "-",
                meta.tool_call_events,
                meta.tool_call_fragments,
                meta.first_reasoning_ms if meta.first_reasoning_ms is not None else "-",
                meta.reasoning_events,
                meta.max_event_gap_ms,
                ",".join(f"{k}:{v}" for k, v in sorted(meta.sse_event_types.items())) or "-",
                ",".join(f"{k}:{v}" for k, v in sorted(meta.delta_key_counts.items())) or "-",
                ",".join(f"{k}:{v}" for k, v in sorted(meta.finish_reason_counts.items())) or "-",
                meta.extra.get("upstream_fields", "-"),
                meta.error_detail or "-",
                error,
            )

        def record(status: str, usage_seen: bool, error: str | None) -> None:
            request_log.record(
                gateway_request_id=gateway_request_id,
                model=model,
                upstream_model=upstream_model,
                status=status,
                usage_seen=usage_seen,
                upstream_request_id=meta.upstream_request_id,
                billing_lookup_id=meta.billing_lookup_id,
                error=error,
                started_at=started,
                ended_at=time.monotonic(),
            )

        try:
            # заявка на сверку живёт ДО ожидания upstream: если процесс упадёт
            # во время генерации, запись с gateway_request_id уже на диске
            record("upstream_inflight", meta.usage_seen, None)
            result = await nanogpt.run_chat(upstream_model, messages, stream, include_usage, extra, meta)
        except nanogpt.UpstreamError as err:
            log_chat(nano_log, *base_args, *meta_args("504" if err.kind == "timeout" else "502", err.kind))
            record("upstream_error", meta.usage_seen, err.kind)
            return _upstream_error(err, gateway_request_id)

        if not stream:
            log_chat(nano_log, *base_args, *meta_args("200", "-"))
            record("succeeded", meta.usage_seen, None)
            headers = {"X-Request-ID": gateway_request_id}
            if meta.upstream_request_id:
                headers["X-Upstream-Request-ID"] = meta.upstream_request_id
            return JSONResponse(result, headers=headers)

        # upstream уже ответил: id известны, фиксируем их в заявке на сверку
        # до того, как стрим начнёт отдавать данные клиенту
        record("upstream_inflight", meta.usage_seen, None)

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
                log_chat(nano_log, *base_args, *meta_args("200", error or "-"))
                if done:
                    record("succeeded", meta.usage_seen, None)
                elif error is None:
                    # генератор закрыт до [DONE] без ошибки: disconnect клиента,
                    # исход платной генерации неизвестен (SPEC_008 §5 шаг 7.4)
                    record("client_disconnected", meta.usage_seen, None)
                elif meta.upstream_request_id:
                    # ошибка после старта, но upstream id известен — нужна сверка
                    record("needs_reconciliation", meta.usage_seen, error)
                else:
                    record("unknown_outcome", meta.usage_seen, error)
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
    result = await stub.run_chat(model, messages, stream, include_usage, extra)

    if not stream:
        log_chat(base_log + " status=200 id=%s", *base_args, result["id"])
        request_log.record(
            gateway_request_id=gateway_request_id,
            model=model,
            upstream_model=None,
            status="succeeded",
            usage_seen=bool(result.get("usage")),
            started_at=started,
            ended_at=time.monotonic(),
        )
        return JSONResponse(result, headers={"X-Request-ID": gateway_request_id})

    async def sse() -> Any:
        request_id = "-"
        done = False
        try:
            async for chunk in result:  # type: ignore[union-attr]
                request_id = chunk.get("id", request_id)
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
            done = True
        finally:
            # лог здесь: request_id известен и при disconnect клиента
            log_chat(base_log + " status=200 id=%s", *base_args, request_id)
            request_log.record(
                gateway_request_id=gateway_request_id,
                model=model,
                upstream_model=None,
                status="succeeded" if done else "client_disconnected",
                usage_seen=True,
                started_at=started,
                ended_at=time.monotonic(),
            )
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
