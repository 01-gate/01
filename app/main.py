import json
import logging
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

MAX_BODY_BYTES = 1_048_576

SECRET_HEADERS = {"authorization", "cookie", "set-cookie", "proxy-authorization", "x-api-key"}
SECRET_KEYS = {"api_key", "apikey", "authorization", "token", "access_token", "secret", "password", "cookie"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("inspector")

app = FastAPI(title="OpenCode request inspector")


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: "<redacted>" if str(k).lower() in SECRET_KEYS else _redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _safe_headers(request: Request) -> dict[str, str]:
    return {k: "<redacted>" if k.lower() in SECRET_HEADERS else v for k, v in request.headers.items()}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/models")
async def list_models() -> dict:
    # shortcut: каталог захардкожен (одна модель), заменить на реестр при маппинге public->upstream ID
    return {
        "object": "list",
        "data": [
            {"id": "01", "object": "model", "created": int(time.time()), "owned_by": "local"},
        ],
    }


@app.post("/v1")
@app.post("/v1/{path:path}")
async def inspect(request: Request) -> JSONResponse:
    path = request.url.path

    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            log.warning("=== %s %s === body_too_large size>%d", request.method, path, MAX_BODY_BYTES)
            return JSONResponse({"received": False, "path": path, "error": "body_too_large"}, status_code=413)

    log.info("=== %s %s ===", request.method, path)
    log.info("content-type=%r body_size=%d", request.headers.get("content-type"), len(body))
    auth = request.headers.get("authorization", "")
    log.info("auth_present=%s auth_prefix=%s", bool(auth), auth[:20] if auth else "")
    log.info("headers=%s", json.dumps(_safe_headers(request), ensure_ascii=False))

    try:
        payload = json.loads(bytes(body))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        # str(JSONDecodeError) есть только позиция, не содержимое тела
        log.error("json_invalid error=%s: %s", type(exc).__name__, exc)
        return JSONResponse({"received": True, "path": path, "json_valid": False}, status_code=400)

    model = payload.get("model") if isinstance(payload, dict) else None
    if model is not None:
        log.info("model=%s", model)
    if isinstance(payload, dict) and "messages" in payload:
        log.info("isinstance")
        #log.info("messages=%s", json.dumps(_redact(payload["messages"]), ensure_ascii=False, indent=2))
    #log.info("payload=%s", json.dumps(_redact(payload), ensure_ascii=False, indent=2))

    return JSONResponse({"received": True, "path": path, "json_valid": True})
