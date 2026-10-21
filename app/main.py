"""Локальный OpenAI-compatible gateway: auth, каталог, Chat Completions (stub)."""

import hmac
import json
import logging
import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.upstream.stub import run_chat

MAX_BODY_BYTES = 1_048_576

PUBLIC_MODELS = {
    "01": {"id": "01", "object": "model", "created": 1729500000, "owned_by": "local"},
}

def _load_dotenv(path: str = ".env") -> None:
    # shortcut: минимальный парсер вместо python-dotenv; только GATEWAY_* и не перезаписывает env
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            key, sep, value = line.strip().partition("=")
            if sep and key.startswith("GATEWAY_"):
                os.environ.setdefault(key, value.strip().strip("\"'"))


_load_dotenv()

logger = logging.getLogger("gateway")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(_handler)
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
app = FastAPI()


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


@app.get("/health")
async def health():
    return JSONResponse({"status": "ok"})


@app.get("/v1/models")
async def list_models():
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
    print(messages)
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

    if model not in PUBLIC_MODELS:
        logger.info(
            "chat method=POST path=/v1/chat/completions status=404 auth_present=%s auth_ok=True model=%s stream=%s",
            auth_present,
            model,
            stream,
        )
        return _error(f"The model `{model}` does not exist", "model_not_found", 404)

    include_usage = bool(stream_options.get("include_usage", False))
    extra_fields = sorted(set(payload) - {"model", "messages", "stream", "stream_options"})
    extra = {k: payload[k] for k in extra_fields}
    result = await run_chat(model, messages, stream, include_usage, extra)

    if not stream:
        logger.info(
            "chat method=POST path=/v1/chat/completions status=200 auth_present=%s auth_ok=True model=%s stream=False messages_count=%s request_id=%s extra_fields=%s",
            auth_present,
            model,
            len(messages),
            result["id"],
            ",".join(extra_fields) or "-",
        )
        return JSONResponse(result)

    async def sse() -> Any:
        request_id = "-"
        try:
            async for chunk in result:
                print(chunk)
                request_id = chunk.get("id", request_id)
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        finally:
            # лог здесь: request_id известен и при disconnect клиента
            logger.info(
                "chat method=POST path=/v1/chat/completions status=200 auth_present=%s auth_ok=True model=%s stream=True messages_count=%s request_id=%s extra_fields=%s",
                auth_present,
                model,
                len(messages),
                request_id,
                ",".join(extra_fields) or "-",
            )

    return StreamingResponse(
        sse(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )
