"""Локальный OpenAI-compatible gateway: auth, каталог, Chat Completions."""

import hmac
import json
import logging
import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app import config
from app.upstream import nanogpt, stub

MAX_BODY_BYTES = 1_048_576

# Публичный allowlist каталога (SPEC_004 §4.1): только контрактные поля,
# upstream-идентификаторы в HTTP-ответ не попадают.
PUBLIC_MODELS = {
    "01": {"id": "01", "object": "model", "created": 1729500000, "owned_by": "local"},
}
# Публичный id -> точный upstream ID из подтверждённого каталога NanoGPT.
MODEL_UPSTREAM = {
    "01": "xiaomi/mimo-v2.6-flash",
}

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


def _error(message: str, code: str, status: int) -> JSONResponse:
    return JSONResponse(
        {"error": {"message": message, "type": "invalid_request_error", "code": code}},
        status_code=status,
    )


def _auth(request: Request) -> tuple[bool, bool]:
    header = request.headers.get("authorization", "")
    auth_present = bool(header)
    token = header[7:].strip() if header.startswith("Bearer ") else ""
    key = _test_key()
    auth_ok = bool(key) and bool(token) and hmac.compare_digest(token, key)
    return auth_present, auth_ok


def _auth_error() -> JSONResponse:
    return _error("Invalid API key", "invalid_api_key", 401)


def _upstream_error(err: nanogpt.UpstreamError) -> JSONResponse:
    # безопасная нормализованная ошибка без тела/заголовков upstream (§7)
    if err.kind == "timeout":
        return _error("Upstream timeout", "upstream_timeout", 504)
    if err.kind == "http":
        return _error("Upstream error", "upstream_error", 502)
    if err.kind == "protocol":
        return _error("Upstream protocol error", "upstream_protocol_error", 502)
    return _error("Upstream unavailable", "upstream_unavailable", 502)


@app.get("/health")
async def health():
    return JSONResponse({"status": "ok"})


@app.get("/v1/models")
async def list_models():
    # публичный endpoint: авторизации нет по решению (§2.4, §4.1)
    return JSONResponse({"object": "list", "data": list(PUBLIC_MODELS.values())})


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    auth_present, auth_ok = _auth(request)
    if not auth_ok:
        logger.info(
            "chat method=POST path=/v1/chat/completions status=401 auth_present=%s auth_ok=False key_set=%s",
            auth_present,
            bool(_test_key()),
        )
        return _auth_error()

    body = bytearray()
    async for part in request.stream():
        body.extend(part)
        if len(body) > MAX_BODY_BYTES:
            logger.info(
                "chat method=POST path=/v1/chat/completions status=413 auth_present=%s auth_ok=True",
                auth_present,
            )
            return _error("Request body too large", "body_too_large", 413)

    try:
        payload = json.loads(body)
    except ValueError:
        logger.info(
            "chat method=POST path=/v1/chat/completions status=400 auth_present=%s auth_ok=True",
            auth_present,
        )
        return _error("Invalid JSON body", "invalid_json", 400)
    if not isinstance(payload, dict):
        logger.info(
            "chat method=POST path=/v1/chat/completions status=400 auth_present=%s auth_ok=True",
            auth_present,
        )
        return _error("Invalid JSON body", "invalid_json", 400)

    model = payload.get("model")
    messages = payload.get("messages")
    stream = payload.get("stream", False)
    stream_options = payload.get("stream_options", {})

    def bad_request(code: str, message: str) -> JSONResponse:
        logger.info(
            "chat method=POST path=/v1/chat/completions status=400 auth_present=%s auth_ok=True model=%s stream=%s",
            auth_present,
            model if isinstance(model, str) else "-",
            stream if isinstance(stream, bool) else "-",
        )
        return _error(message, code, 400)

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

    if model not in PUBLIC_MODELS or model not in MODEL_UPSTREAM:
        # каталог и маппинг обязаны совпадать; atomic-проверка до вызова upstream
        logger.info(
            "chat method=POST path=/v1/chat/completions status=404 auth_present=%s auth_ok=True model=%s stream=%s upstream_call=False",
            auth_present,
            model,
            stream,
        )
        return _error(f"The model `{model}` does not exist", "model_not_found", 404)

    include_usage = bool(stream_options.get("include_usage", False))
    extra_fields = sorted(set(payload) - {"model", "messages", "stream", "stream_options"})
    extra = {k: payload[k] for k in extra_fields}
    base_log = (
        "chat method=POST path=/v1/chat/completions auth_present=%s auth_ok=True model=%s "
        "upstream_model=%s stream=%s messages_count=%s extra_fields=%s"
    )
    base_args = (
        auth_present,
        model,
        MODEL_UPSTREAM.get(model, model) if _upstream_mode() == "nanogpt" else "-",
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
        " sse_event_types=%s delta_keys=%s upstream_fields=%s"
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
                meta.extra.get("upstream_fields", "-"),
                meta.error_detail or "-",
                error,
            )

        try:
            result = await nanogpt.run_chat(
                MODEL_UPSTREAM[model], messages, stream, include_usage, extra, meta
            )
        except nanogpt.UpstreamError as err:
            logger.info(
                nano_log,
                *base_args,
                *meta_args("504" if err.kind == "timeout" else "502", err.kind),
            )
            return _upstream_error(err)

        if not stream:
            logger.info(nano_log, *base_args, *meta_args("200", "-"))
            return JSONResponse(result)

        async def sse_nano() -> Any:
            error: str | None = None
            try:
                async for data in result:  # type: ignore[union-attr]
                    yield f"data: {data}\n\n"
                yield "data: [DONE]\n\n"
            except nanogpt.UpstreamError as err:
                # статус уже отправлен: обрываем поток честно, без повторов (§6, §7)
                error = err.kind
            except Exception as exc:
                # неожиданный сбой после отправки статуса: фиксируем в логе,
                # а не выдаём за штатное завершение (SPEC_006 §2)
                error = type(exc).__name__
            finally:
                logger.info(nano_log, *base_args, *meta_args("200", error or "-"))

        return StreamingResponse(
            sse_nano(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    # legacy stub-режим: прежний интерфейс и поведение (§2.11)
    result = await stub.run_chat(model, messages, stream, include_usage, extra)

    if not stream:
        logger.info(base_log + " status=200 id=%s", *base_args, result["id"])
        return JSONResponse(result)

    async def sse() -> Any:
        request_id = "-"
        try:
            async for chunk in result:  # type: ignore[union-attr]
                request_id = chunk.get("id", request_id)
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        finally:
            # лог здесь: request_id известен и при disconnect клиента
            logger.info(
                base_log + " status=200 id=%s", *base_args, request_id
            )

    return StreamingResponse(
        sse(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )