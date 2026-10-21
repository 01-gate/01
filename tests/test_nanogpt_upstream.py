"""SPEC_004 §10.3–§10.4: upstream NanoGPT через mock transport, без сети и затрат."""

import asyncio
import json
import logging
import time

import httpx
import pytest

from app import main as gw
from app.upstream import nanogpt

GATEWAY_KEY = "test-gateway-key-123"
NANO_KEY = "sk-nano-test-upstream"
AUTH = {"Authorization": f"Bearer {GATEWAY_KEY}"}
NANO_BASE = "https://api.nano-gpt.com/v1"

SYNC_BODY = {
    "id": "chatcmpl-up-1",
    "object": "chat.completion",
    "created": 1729500000,
    "model": "xiaomi/mimo-v2.6-flash",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "pong"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    "x_nanogpt_pricing": {"amount": 0.000001, "currency": "USD", "requestId": "req_PRICE"},
}

STREAM_CHUNKS = [
    json.dumps(
        {
            "id": "chatcmpl-up-1",
            "object": "chat.completion.chunk",
            "created": 1729500000,
            "model": "xiaomi/mimo-v2.6-flash",
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": "pon"}, "finish_reason": None}],
        }
    ),
    json.dumps(
        {
            "id": "chatcmpl-up-1",
            "object": "chat.completion.chunk",
            "created": 1729500000,
            "model": "xiaomi/mimo-v2.6-flash",
            "choices": [{"index": 0, "delta": {"content": "g"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
            "x_nanogpt_pricing": {"amount": 0.000001, "currency": "USD", "requestId": "req_PRICE"},
        }
    ),
]


class FakeUpstream:
    """Минимальное ASGI-приложение вместо NanoGPT; mode меняется на лету."""

    def __init__(self):
        self.mode = "ok"  # ok | http_500 | malformed | sync_no_usage | stream_no_usage
        self.stream_delay = 0.0
        self.requests = []  # {"path":..., "headers":..., "payload":...}

    async def __call__(self, scope, receive, send):
        body = b""
        more = True
        while more:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                return
            body += msg.get("body", b"")
            more = msg.get("more_body", False)
        headers = {k.decode(): v.decode() for k, v in scope["headers"]}
        payload = json.loads(body) if body else {}
        self.requests.append(
            {"path": scope["path"], "headers": headers, "payload": payload}
        )
        if self.mode == "http_500":
            message = b'SECRET_UPSTREAM_RAW_BODY'
            await send(
                {
                    "type": "http.response.start",
                    "status": 500,
                    "headers": [
                        (b"content-type", b"text/plain"),
                        (b"x-request-id", b"req_ERR"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": message})
            return
        if self.mode == "malformed":
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/plain")],
                }
            )
            await send({"type": "http.response.body", "body": b"not-json-at-all"})
            return
        if self.mode == "sync_no_usage":
            body = {k: v for k, v in SYNC_BODY.items() if k != "usage"}
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": json.dumps(body).encode()})
            return
        # обычные ответы
        if payload.get("stream"):
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"text/event-stream"),
                        (b"x-request-id", b"req_HEADER"),
                    ],
                }
            )
            chunks = list(STREAM_CHUNKS)
            if self.mode == "stream_no_usage":
                chunks = [json.loads(c) for c in chunks]
                for c in chunks:
                    c.pop("usage", None)
                chunks = [json.dumps(c) for c in chunks]
            parts = [f"data: {c}\n\n" for c in chunks] + ["data: [DONE]\n\n"]
            for i, part in enumerate(parts):
                if self.stream_delay and i > 0:
                    await asyncio.sleep(self.stream_delay)
                await send(
                    {
                        "type": "http.response.body",
                        "body": part.encode(),
                        "more_body": i < len(parts) - 1,
                    }
                )
            return
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"x-request-id", b"req_HEADER"),
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": json.dumps(SYNC_BODY).encode(),
            }
        )


class BoomTransport(httpx.AsyncBaseTransport):
    """Транспорт, имитирующий сетевые ошибки до/после отправки."""

    def __init__(self, exc):
        self.exc = exc

    async def handle_async_request(self, request):
        raise self.exc


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _start_server(app_obj, port):
    import uvicorn

    config = uvicorn.Config(app_obj, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    return server, task


@pytest.fixture(autouse=True)
def _nano_env(monkeypatch):
    monkeypatch.setenv("GATEWAY_UPSTREAM", "nanogpt")
    monkeypatch.setenv("GATEWAY_TEST_API_KEY", GATEWAY_KEY)
    monkeypatch.setenv("NANOGPT_API_KEY", NANO_KEY)
    monkeypatch.setenv("NANOGPT_INFERENCE_BASE_URL", NANO_BASE)
    monkeypatch.setenv("NANOGPT_USAGE_BASE_URL", "https://api.nano-gpt.com/api/v1")


@pytest.fixture
def fake(monkeypatch):
    upstream = FakeUpstream()

    def factory():
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=upstream), base_url=NANO_BASE
        )

    monkeypatch.setattr(nanogpt, "_client", factory)
    return upstream


def request(method, path, **kwargs):
    async def _do():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gw.app), base_url="http://test"
        ) as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(_do())


def _chat(**payload):
    return request("POST", "/v1/chat/completions", headers=AUTH, json=payload)


# §10.3.9–10.3.13


def test_client_base_url_from_env():
    # url собирается только из NANOGPT_INFERENCE_BASE_URL (§5.1/10.2.9)
    client = nanogpt._client()
    try:
        assert str(client.base_url).rstrip("/") == NANO_BASE
    finally:
        asyncio.run(client.aclose())


def test_sync_proxied_with_separate_ids(fake, caplog):
    caplog.set_level(logging.INFO, logger="gateway")
    r = _chat(model="01", messages=[{"role": "user", "content": "hi"}])
    assert r.status_code == 200
    body = r.json()
    # §10.3.11/12: форма проксируется, id не подменяется заголовком
    assert body["id"] == "chatcmpl-up-1"
    assert body["object"] == "chat.completion"
    assert body["usage"]["total_tokens"] == 8
    # §5.4: внутренняя себестоимость скрыта
    assert "x_nanogpt_pricing" not in body
    # §5.3/10.3.13: все идентификаторы в логе раздельно и непусты
    line = [l for l in caplog.messages if "response_id=" in l][-1]
    assert "response_id=chatcmpl-up-1" in line
    assert "upstream_request_id=req_HEADER" in line
    assert "billing_lookup_id=req_PRICE" in line
    assert fake.requests[0]["path"] == "/v1/chat/completions"


def test_usage_base_url_never_used(fake):
    r = _chat(model="01", messages=[{"role": "user", "content": "hi"}])
    assert r.status_code == 200
    paths = [req["path"] for req in fake.requests]
    assert paths == ["/v1/chat/completions"]  # GET /usage и billing не вызываются
    assert all("/usage" not in p for p in paths)


def test_upstream_signed_with_nanogpt_key_only(fake):
    r = _chat(model="01", messages=[{"role": "user", "content": "hi"}])
    assert r.status_code == 200
    headers = fake.requests[0]["headers"]
    assert headers["authorization"] == f"Bearer {NANO_KEY}"
    assert GATEWAY_KEY not in json.dumps(headers)


def test_upstream_signed_on_stream_too(fake):
    # регрессия: стрим-запрос должен подписываться NANOGPT_API_KEY, как и sync
    got = _first_stream_data()
    assert got[-1][1] == "[DONE]"
    headers = fake.requests[0]["headers"]
    assert headers["authorization"] == f"Bearer {NANO_KEY}"


def test_client_model_mapped_to_upstream_id(fake):
    r = _chat(model="01", messages=[{"role": "user", "content": "hi"}])
    assert r.status_code == 200
    assert fake.requests[0]["payload"]["model"] == "xiaomi/mimo-v2.6-flash"


def test_401_never_calls_upstream(fake):
    for headers in ({}, {"Authorization": "Bearer wrong"}):
        r = request("POST", "/v1/chat/completions", headers=headers, json={
            "model": "01", "messages": [{"role": "user", "content": "hi"}]
        })
        assert r.status_code == 401
    assert fake.requests == []


def test_unknown_model_never_calls_upstream(fake):
    r = _chat(model="nope", messages=[{"role": "user", "content": "hi"}])
    assert r.status_code == 404
    assert fake.requests == []


# §10.3.14 streaming


def _first_stream_data(chunk_hook=None):
    async def _do():
        t0 = time.monotonic()
        got = []
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gw.app), base_url="http://test"
        ) as client:
            async with client.stream(
                "POST",
                "/v1/chat/completions",
                headers=AUTH,
                json={
                    "model": "01",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                    "stream_options": {"include_usage": True},
                },
            ) as r:
                assert r.headers["content-type"].startswith("text/event-stream")
                async for line in r.aiter_lines():
                    if line.startswith("data: "):
                        got.append((time.monotonic() - t0, line[6:]))
                        if chunk_hook:
                            chunk_hook(len(got))
                        if got[-1][1] == "[DONE]":
                            break
        return got

    return asyncio.run(_do())


def test_stream_events_order_pricing_stripped_done(fake, caplog):
    caplog.set_level(logging.INFO, logger="gateway")
    got = _first_stream_data()
    assert got[-1][1] == "[DONE]"
    assert sum(1 for _, d in got if d == "[DONE]") == 1
    chunks = [json.loads(d) for _, d in got if d != "[DONE]"]
    assert all("x_nanogpt_pricing" not in c for c in chunks)
    assert chunks[-1]["usage"]["total_tokens"] == 8
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert fake.requests[0]["payload"]["stream"] is True
    line = [l for l in caplog.messages if "billing_lookup_id=req_PRICE" in l]
    assert line  # billing id извлечён из финального чанка потока


def test_stream_not_fully_buffered_real_socket(monkeypatch):
    # §10.3.14. httpx.ASGITransport буферит ответ на своей стороне,
    # поэтому доказательством служат реальные локальные сокеты:
    # первый SSE-событие должно прийти раньше конца upstream-потока.
    fake = FakeUpstream()
    fake.stream_delay = 0.4  # итого ~0.8s потока

    up_port = _free_port()
    gw_port = _free_port()

    def factory():
        return httpx.AsyncClient(base_url=f"http://127.0.0.1:{up_port}")

    monkeypatch.setattr(nanogpt, "_client", factory)
    monkeypatch.setenv("NANOGPT_INFERENCE_BASE_URL", f"http://127.0.0.1:{up_port}")

    async def go():
        gw_server, gw_task = await _start_server(gw.app, gw_port)
        up_server, up_task = await _start_server(fake, up_port)
        try:
            t0 = time.monotonic()
            first_at = None
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{gw_port}", timeout=10.0
            ) as client:
                async with client.stream(
                    "POST",
                    "/v1/chat/completions",
                    headers=AUTH,
                    json={
                        "model": "01",
                        "messages": [{"role": "user", "content": "hi"}],
                        "stream": True,
                    },
                ) as r:
                    async for line in r.aiter_lines():
                        if line.startswith("data: ") and first_at is None:
                            first_at = time.monotonic() - t0
                        if line == "data: [DONE]":
                            done_at = time.monotonic() - t0
                            break
            return first_at, done_at
        finally:
            for server, task in ((gw_server, gw_task), (up_server, up_task)):
                server.should_exit = True
                await task

    first_at, done_at = asyncio.run(go())
    assert first_at is not None
    # при буферизации оба времени совпали бы; здесь событие 1 задолго до конца
    assert first_at + 0.3 < done_at, (
        f"первое событие {first_at:.3f}s при полном потоке {done_at:.3f}s — буферизация?"
    )


def test_stream_usage_missing_is_visible(fake, caplog):
    caplog.set_level(logging.INFO, logger="gateway")
    fake.mode = "stream_no_usage"
    got = _first_stream_data()
    assert got[-1][1] == "[DONE]"
    line = [l for l in caplog.messages if l.startswith("chat method=POST")][-1]
    assert "usage=missing" in line  # не «бесплатно», а наблюдение для сверки


def test_stream_client_disconnect_no_retry(fake):
    async def _do():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gw.app), base_url="http://test"
        ) as client:
            async with client.stream(
                "POST",
                "/v1/chat/completions",
                headers=AUTH,
                json={"model": "01", "messages": [{"role": "user", "content": "hi"}], "stream": True},
            ) as r:
                async for line in r.aiter_lines():
                    if line.startswith("data: "):
                        break  # клиент ушёл после первого события

    asyncio.run(_do())
    assert len(fake.requests) == 1  # повтора генерации не было


# §10.3.15 ошибки


def _patch_transport(monkeypatch, exc):
    def factory():
        return httpx.AsyncClient(transport=BoomTransport(exc), base_url=NANO_BASE)

    monkeypatch.setattr(nanogpt, "_client", factory)


def test_upstream_http_error_normalized(fake, capfd):
    capfd.readouterr()  # сброс буфера
    fake.mode = "http_500"
    r = _chat(model="01", messages=[{"role": "user", "content": "hi"}])
    assert r.status_code == 502
    body = r.json()
    assert body["error"]["code"] == "upstream_error"
    assert "SECRET_UPSTREAM_RAW_BODY" not in r.text  # сырое тело не раскрывается
    captured = capfd.readouterr()
    assert "SECRET_UPSTREAM_RAW_BODY" not in captured.out + captured.err


def test_connect_error_maps_to_502(monkeypatch):
    _patch_transport(monkeypatch, httpx.ConnectError("refused"))
    r = _chat(model="01", messages=[{"role": "user", "content": "hi"}])
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "upstream_unavailable"


def test_timeout_maps_to_504_without_retry(monkeypatch):
    calls = []

    def factory():
        calls.append(1)
        return httpx.AsyncClient(transport=BoomTransport(httpx.ReadTimeout("slow")), base_url=NANO_BASE)

    monkeypatch.setattr(nanogpt, "_client", factory)
    r = _chat(model="01", messages=[{"role": "user", "content": "hi"}])
    assert r.status_code == 504
    assert r.json()["error"]["code"] == "upstream_timeout"
    assert len(calls) == 1  # слепого повтора не было


def test_malformed_upstream_response(fake):
    fake.mode = "malformed"
    r = _chat(model="01", messages=[{"role": "user", "content": "hi"}])
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "upstream_protocol_error"


# §10.4 логи и секреты


def test_no_secrets_or_prompts_in_output(fake, caplog, capfd):
    caplog.set_level(logging.INFO, logger="gateway")
    capfd.readouterr()
    prompt = "TOP_SECRET_PROMPT_9f3"
    r = _chat(model="01", messages=[{"role": "user", "content": prompt}])
    assert r.status_code == 200
    r2 = request("POST", "/v1/chat/completions", json={"model": "01", "messages": []})
    assert r2.status_code == 401
    fake.mode = "http_500"
    r3 = _chat(model="01", messages=[{"role": "user", "content": prompt}])
    assert r3.status_code == 502
    captured = capfd.readouterr()
    haystack = caplog.text + captured.out + captured.err
    assert prompt not in haystack
    assert GATEWAY_KEY not in haystack
    assert NANO_KEY not in haystack
    assert "authorization" not in haystack.lower() or "Bearer" not in haystack

# §10.3.16–17 неопределённые исходы


def test_sync_missing_usage_visible_not_free(fake, caplog):
    caplog.set_level(logging.INFO, logger="gateway")
    fake.mode = "sync_no_usage"
    r = _chat(model="01", messages=[{"role": "user", "content": "hi"}])
    assert r.status_code == 200
    assert "usage" not in r.json()
    line = [l for l in caplog.messages if "response_id=chatcmpl-up-1" in l][-1]
    assert "usage=missing" in line  # наблюдение, а не «бесплатный запрос»


def test_usage_base_url_not_used_by_runtime():
    # §10.2.10: настройка загружается конфигом, но код среза её не вызывает
    from pathlib import Path

    root = Path(gw.__file__).parent
    sources = [root / "main.py", root / "upstream" / "nanogpt.py", root / "upstream" / "stub.py"]
    for src in sources:
        assert "NANOGPT_USAGE_BASE_URL" not in src.read_text(encoding="utf-8"), src
