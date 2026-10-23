"""SPEC_004 §10.3–§10.4: upstream NanoGPT через mock transport, без сети и затрат."""

import asyncio
import json
import logging
import time

import httpx
import pytest

from app import config, limits
from app import main as gw
from app.models_catalog import MODEL_UPSTREAM
from app.upstream import nanogpt

GATEWAY_KEY = "test-gateway-key-123"
NANO_KEY = "sk-nano-test-upstream"
AUTH = {"Authorization": f"Bearer {GATEWAY_KEY}"}
NANO_BASE = "https://api.nano-gpt.com/v1"
TEST_MODEL = config.test_model()
TEST_UPSTREAM_MODEL = MODEL_UPSTREAM[TEST_MODEL]

SYNC_BODY = {
    "id": "chatcmpl-up-1",
    "object": "chat.completion",
    "created": 1729500000,
    "model": TEST_UPSTREAM_MODEL,
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
            "model": TEST_UPSTREAM_MODEL,
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": "pon"}, "finish_reason": None}],
        }
    ),
    json.dumps(
        {
            "id": "chatcmpl-up-1",
            "object": "chat.completion.chunk",
            "created": 1729500000,
            "model": TEST_UPSTREAM_MODEL,
            "choices": [{"index": 0, "delta": {"content": "g"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
            "x_nanogpt_pricing": {"amount": 0.000001, "currency": "USD", "requestId": "req_PRICE"},
        }
    ),
]


class FakeUpstream:
    """Минимальное ASGI-приложение вместо NanoGPT; mode меняется на лету."""

    def __init__(self):
        self.mode = "ok"  # ok | http_500 | http_status | malformed | sync_no_usage | stream_no_usage
        self.status = 500  # используется в режиме http_status
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
            await self._send_error(send, 500)
            return
        if self.mode == "http_status":
            await self._send_error(send, self.status)
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
        if self.mode in {"sync_no_usage", "sync_nothing"}:
            drop = {"usage"} if self.mode == "sync_no_usage" else {"usage", "x_nanogpt_pricing"}
            body = {k: v for k, v in SYNC_BODY.items() if k not in drop}
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
            if self.mode == "tool_stream":
                chunks = [
                    json.dumps(
                        {
                            "id": "chatcmpl-up-1",
                            "object": "chat.completion.chunk",
                            "created": 1729500000,
                            "model": TEST_UPSTREAM_MODEL,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {
                                        "tool_calls": [
                                            {
                                                "index": 0,
                                                "id": "call_1",
                                                "type": "function",
                                                "function": {
                                                    "name": "read_file",
                                                    "arguments": '{"path":',
                                                },
                                            }
                                        ]
                                    },
                                    "finish_reason": "tool_calls",
                                }
                            ],
                        }
                    )
                ]
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

    async def _send_error(self, send, status: int):
        # тело с секретом: не должно утечь клиенту/в логи (§4.2.A.4)
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"text/plain"),
                    (b"x-request-id", b"req_ERR"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b"SECRET_UPSTREAM_RAW_BODY"})


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
def _nano_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GATEWAY_UPSTREAM", "nanogpt")
    monkeypatch.setenv("GATEWAY_TEST_API_KEY", GATEWAY_KEY)
    monkeypatch.setenv("NANOGPT_API_KEY", NANO_KEY)
    monkeypatch.setenv("NANOGPT_INFERENCE_BASE_URL", NANO_BASE)
    monkeypatch.setenv("NANOGPT_USAGE_BASE_URL", "https://api.nano-gpt.com/api/v1")
    monkeypatch.setenv("GATEWAY_REQUEST_LOG_PATH", str(tmp_path / "request_log.jsonl"))
    limits.reset()
    yield
    limits.reset()


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
    r = _chat(model=TEST_MODEL, messages=[{"role": "user", "content": "hi"}])
    assert r.status_code == 200
    assert r.headers["x-upstream-request-id"] == "req_HEADER"
    body = r.json()
    # §10.3.11/12: форма проксируется, id не подменяется заголовком
    assert body["id"] == "chatcmpl-up-1"
    assert body["object"] == "chat.completion"
    assert body["usage"]["total_tokens"] == 8
    # §5.4: внутренняя себестоимость скрыта
    assert "x_nanogpt_pricing" not in body
    # §5.3/10.3.13: все идентификаторы в логе раздельно и непусты
    line = [l for l in caplog.messages if l.startswith("chat ")][-1]
    assert "upstream_id=chatcmpl-up-1" in line
    assert "upstream_request_id=req_HEADER" in line
    assert "billing_lookup_id=req_PRICE" in line
    assert fake.requests[0]["path"] == "/v1/chat/completions"


def test_forwarded_parameters_are_explicit_and_reasoning_effort_is_forwarded(fake):
    # NanoGPT documents reasoning_effort and reasoning controls for Chat Completions.
    r = _chat(
        model=TEST_MODEL,
        messages=[{"role": "user", "content": "hi"}],
        temperature=0.2,
        max_completion_tokens=123,
        reasoning_effort="high",
        reasoning={"exclude": True},
        unsupported_client_option="not-forwarded",
    )
    assert r.status_code == 200
    sent = fake.requests[-1]["payload"]
    assert sent["temperature"] == 0.2
    assert sent["max_completion_tokens"] == 123
    assert sent["reasoning_effort"] == "high"
    assert sent["reasoning"] == {"exclude": True}
    assert "unsupported_client_option" not in sent
    assert "messages" in sent and "model" in sent
    assert set(sent) == {
        "model", "messages", "temperature", "max_completion_tokens",
        "reasoning_effort", "reasoning",
    }


def test_tool_calling_fields_forwarded_to_upstream(fake):
    tools = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        }
    ]
    messages = [
        {"role": "user", "content": "Read README.md"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"path": "README.md"}),
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "contents"},
    ]
    r = _chat(
        model=TEST_MODEL,
        messages=messages,
        tools=tools,
        tool_choice="auto",
        parallel_tool_calls=False,
    )
    assert r.status_code == 200
    sent = fake.requests[-1]["payload"]
    assert sent["tools"] == tools
    assert sent["tool_choice"] == "auto"
    assert sent["parallel_tool_calls"] is False
    assert sent["messages"] == messages


def test_stream_tool_call_delta_is_preserved(fake, caplog):
    caplog.set_level(logging.DEBUG, logger="gateway")
    fake.mode = "tool_stream"
    r = _chat(
        model=TEST_MODEL,
        messages=[{"role": "user", "content": "Read README.md"}],
        stream=True,
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
                },
            }
        ],
    )
    assert r.status_code == 200
    assert r.headers["x-upstream-request-id"] == "req_HEADER"
    events = [line[6:] for line in r.text.splitlines() if line.startswith("data: ")]
    chunks = [json.loads(event) for event in events if event != "[DONE]"]
    tool_chunks = [
        call
        for chunk in chunks
        for choice in chunk.get("choices", [])
        for call in choice.get("delta", {}).get("tool_calls", [])
    ]
    assert tool_chunks
    assert tool_chunks[0]["function"]["name"] == "read_file"
    assert events[-1] == "[DONE]"
    info_lines = [l for l in caplog.messages if l.startswith(("chat ", "tokens "))]
    debug_line = [l for l in caplog.messages if l.startswith("chat_debug ")][-1]
    assert all("delta_keys" not in l and "tool_call_events" not in l for l in info_lines)
    assert "tool_call_events=1" in debug_line
    assert "tool_call_fragments=1" in debug_line
    assert "first_tool_call_ms=" in debug_line
    assert "finish_reasons=tool_calls:1" in debug_line
    assert "TOP_SECRET" not in debug_line


def test_tool_call_roundtrip_is_client_driven_and_not_retried(fake):
    # A tool call is returned to the client; the client submits its result in a new request.
    # The gateway must preserve both turns and must not execute/retry tool calls itself.
    fake.mode = "tool_stream"
    tools = [{
        "type": "function",
        "function": {
            "name": "read_file",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    }]
    first = _chat(
        model=TEST_MODEL,
        messages=[{"role": "user", "content": "Read README.md"}],
        stream=True,
        tools=tools,
        tool_choice="auto",
    )
    assert first.status_code == 200
    chunks = [
        json.loads(line[6:])
        for line in first.text.splitlines()
        if line.startswith("data: ") and line[6:] != "[DONE]"
    ]
    calls = [
        call
        for chunk in chunks
        for choice in chunk.get("choices", [])
        for call in choice.get("delta", {}).get("tool_calls", [])
    ]
    assert calls and calls[0]["function"]["name"] == "read_file"

    fake.mode = "ok"
    messages = [
        {"role": "user", "content": "Read README.md"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": json.dumps({"path": "README.md"}),
                },
            }],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "README contents"},
    ]
    second = _chat(model=TEST_MODEL, messages=messages, tools=tools)
    assert second.status_code == 200
    assert len(fake.requests) == 2
    assert fake.requests[0]["payload"]["stream"] is True
    assert fake.requests[1]["payload"]["messages"] == messages
    assert fake.requests[1]["payload"]["tools"] == tools


def test_usage_base_url_never_used(fake):
    r = _chat(model=TEST_MODEL, messages=[{"role": "user", "content": "hi"}])
    assert r.status_code == 200
    paths = [req["path"] for req in fake.requests]
    assert paths == ["/v1/chat/completions"]  # GET /usage и billing не вызываются
    assert all("/usage" not in p for p in paths)


def test_upstream_signed_with_nanogpt_key_only(fake):
    r = _chat(model=TEST_MODEL, messages=[{"role": "user", "content": "hi"}])
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
    r = _chat(model=TEST_MODEL, messages=[{"role": "user", "content": "hi"}])
    assert r.status_code == 200
    assert fake.requests[0]["payload"]["model"] == TEST_UPSTREAM_MODEL


def test_401_never_calls_upstream(fake):
    for headers in ({}, {"Authorization": "Bearer wrong"}):
        r = request("POST", "/v1/chat/completions", headers=headers, json={
            "model": TEST_MODEL, "messages": [{"role": "user", "content": "hi"}]
        })
        assert r.status_code == 401
    assert fake.requests == []


def test_unknown_model_never_calls_upstream(fake):
    r = _chat(model="nope", messages=[{"role": "user", "content": "hi"}])
    assert r.status_code == 404
    assert fake.requests == []


def test_only_catalog_keys_are_accepted(fake):
    # SPEC_008 §5 шаг 1: accept-list chat атомарен каталогу; прежние лишние ключи
    # (например luna) и upstream ID как публичный ID не принимаются
    for bad in ("nope", TEST_UPSTREAM_MODEL, "01"):
        r = _chat(model=bad, messages=[{"role": "user", "content": "hi"}])
        assert r.status_code == 404, bad
    assert fake.requests == []
    assert TEST_MODEL in MODEL_UPSTREAM


def test_concurrency_limit_rejects_before_upstream(fake, monkeypatch):
    # SPEC_008 §5 шаг 6: превышение concurrency даёт 429 до upstream
    monkeypatch.setenv("GATEWAY_MAX_CONCURRENT_CHAT", "1")
    fake.stream_delay = 0.2  # первый запрос удерживает слот во время стрима

    async def go():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gw.app), base_url="http://test"
        ) as client:
            async def one():
                return await client.post(
                    "/v1/chat/completions",
                    headers=AUTH,
                    json={
                        "model": TEST_MODEL,
                        "messages": [{"role": "user", "content": "hi"}],
                        "stream": True,
                    },
                )

            return await asyncio.gather(one(), one())

    r1, r2 = asyncio.run(go())
    assert sorted([r1.status_code, r2.status_code]) == [200, 429]
    assert len(fake.requests) == 1  # на отклонённом upstream не вызывался


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
                    "model": TEST_MODEL,
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
                        "model": TEST_MODEL,
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
    line = [l for l in caplog.messages if l.startswith("tokens ")][-1]
    assert "usage=missing" in line  # не «бесплатно», а наблюдение для сверки
    # pricing в чанке известен, поэтому cost не unknown; usage всё ещё missing
    assert "cost=0.000001" in line
    assert "cost_seen=1" in line


def test_stream_client_disconnect_no_retry(fake):
    async def _do():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gw.app), base_url="http://test"
        ) as client:
            async with client.stream(
                "POST",
                "/v1/chat/completions",
                headers=AUTH,
                json={"model": TEST_MODEL, "messages": [{"role": "user", "content": "hi"}], "stream": True},
            ) as r:
                async for line in r.aiter_lines():
                    if line.startswith("data: "):
                        break  # клиент ушёл после первого события

    asyncio.run(_do())
    assert len(fake.requests) == 1  # повтора генерации не было
    # NB: httpx.ASGITransport буферизует ответ, поэтому реальный disconnect
    # клиента здесь не воспроизводится; незавершённый поток покрыт ниже
    # тестом на реальных сокетах (test_stream_error_after_start...).


def test_client_disconnect_closes_upstream_resources(fake, monkeypatch):
    # §4.5.8/§9: отключение клиента до конца потока закрывает upstream-ресурсы
    closed = []

    class TrackedClient(httpx.AsyncClient):
        async def aclose(self):
            closed.append(True)
            await super().aclose()

    def factory():
        return TrackedClient(
            transport=httpx.ASGITransport(app=fake), base_url=NANO_BASE
        )

    monkeypatch.setattr(nanogpt, "_client", factory)

    async def go():
        gen = await nanogpt.run_chat(
            TEST_UPSTREAM_MODEL,
            [{"role": "user", "content": "hi"}],
            True,
            False,
            {},
            nanogpt.UpstreamMeta(),
        )
        first = await anext(gen)
        await gen.aclose()  # отключение клиента до штатного конца
        assert first

    asyncio.run(go())
    assert closed == [True]  # клиент upstream закрыт ровно один раз


# §10.3.15 ошибки


def _patch_transport(monkeypatch, exc):
    def factory():
        return httpx.AsyncClient(transport=BoomTransport(exc), base_url=NANO_BASE)

    monkeypatch.setattr(nanogpt, "_client", factory)


def test_upstream_http_error_normalized(fake, capfd):
    capfd.readouterr()  # сброс буфера
    fake.mode = "http_500"
    r = _chat(model=TEST_MODEL, messages=[{"role": "user", "content": "hi"}])
    assert r.status_code == 502
    assert r.headers["x-request-id"]  # ошибка тоже сопоставима с логом
    body = r.json()
    assert body["error"]["code"] == "upstream_error"
    assert "SECRET_UPSTREAM_RAW_BODY" not in r.text  # сырое тело не раскрывается
    captured = capfd.readouterr()
    assert "SECRET_UPSTREAM_RAW_BODY" not in captured.out + captured.err


def test_connect_error_maps_to_502(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="gateway")
    _patch_transport(monkeypatch, httpx.ConnectError("refused"))
    r = _chat(model=TEST_MODEL, messages=[{"role": "user", "content": "hi"}])
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "upstream_unavailable"
    # SPEC_006 §5: ошибочный исход логируется с фактической длительностью
    # (мгновенный отказ честно даёт duration_ms=0 — быть ненулевой он обязан
    # только при реальной задержке, что проверяет тест 613)
    line = [l for l in caplog.messages if l.startswith("chat ")][-1]
    assert "error=connect" in line
    assert "outcome=upstream_error" in line
    assert any(l.startswith("tokens ") and "usage=missing" in l for l in caplog.messages)


def test_timeout_maps_to_504_without_retry(monkeypatch):
    calls = []

    def factory():
        calls.append(1)
        return httpx.AsyncClient(transport=BoomTransport(httpx.ReadTimeout("slow")), base_url=NANO_BASE)

    monkeypatch.setattr(nanogpt, "_client", factory)
    r = _chat(model=TEST_MODEL, messages=[{"role": "user", "content": "hi"}])
    assert r.status_code == 504
    assert r.headers["x-request-id"]
    assert r.json()["error"]["code"] == "upstream_timeout"
    assert len(calls) == 1  # слепого повтора не было


def test_connect_timeout_maps_to_504_not_502(monkeypatch):
    # SPEC_005 дефект №3: ConnectTimeout — таймаут, а не ошибка соединения
    _patch_transport(monkeypatch, httpx.ConnectTimeout("slow"))
    r = _chat(model=TEST_MODEL, messages=[{"role": "user", "content": "hi"}])
    assert r.status_code == 504
    assert r.headers["x-request-id"]
    assert r.json()["error"]["code"] == "upstream_timeout"


# §5.3 SPEC_005: классификация всех таймаутов — явные непересекающиеся правила


def test_classify_all_timeout_types_and_connect_error():
    for exc in (
        httpx.ConnectTimeout("t"),
        httpx.ReadTimeout("t"),
        httpx.WriteTimeout("t"),
        httpx.PoolTimeout("t"),
    ):
        assert nanogpt._classify(exc).kind == "timeout", type(exc).__name__
    assert nanogpt._classify(httpx.ConnectError("refused")).kind == "connect"
    # SPEC_006: обрыв протокола при живом соединении — protocol, не connect
    broken = httpx.RemoteProtocolError("peer closed connection")
    assert nanogpt._classify(broken).kind == "protocol"


@pytest.mark.parametrize(
    "exc",
    [httpx.ConnectTimeout("t"), httpx.ReadTimeout("t"), httpx.WriteTimeout("t"), httpx.PoolTimeout("t")],
)
def test_all_timeout_types_map_to_504(monkeypatch, exc):
    # публичный статус: любой таймаут до начала ответа — 504, без сети
    _patch_transport(monkeypatch, exc)
    r = _chat(model=TEST_MODEL, messages=[{"role": "user", "content": "hi"}])
    assert r.status_code == 504
    assert r.headers["x-request-id"]
    assert r.json()["error"]["code"] == "upstream_timeout"


def test_write_and_pool_timeout_classified_as_timeout():
    # отдельная проверка бывших «серых» типов (§5.2 особая обработка объяснимо)
    assert nanogpt._classify(httpx.WriteTimeout("t")).kind == "timeout"
    assert nanogpt._classify(httpx.PoolTimeout("t")).kind == "timeout"


# §4.5 SPEC_005: ошибки upstream до начала ответа клиенту


def _stream_chat():
    return request(
        "POST",
        "/v1/chat/completions",
        headers=AUTH,
        json={"model": TEST_MODEL, "messages": [{"role": "user", "content": "hi"}], "stream": True},
    )


@pytest.mark.parametrize("bad_status", [401, 429, 500])
def test_stream_upstream_error_before_body_is_gateway_error(fake, bad_status):
    # §4.5.1–2: ответ ошибочной стадии до начала передачи — настоящий
    # HTTP-статус gateway (502), а не 200 с обрывом; тело не раскрывается
    fake.mode = "http_status"
    fake.status = bad_status
    r = _stream_chat()
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "upstream_error"
    assert "SECRET_UPSTREAM_RAW_BODY" not in r.text
    assert not r.headers["content-type"].startswith("text/event-stream")
    assert len(fake.requests) == 1  # второй запрос ради предпроверки не открывался


def test_stream_connect_error_before_body_returns_502(monkeypatch, caplog):
    # §4.5.3: ошибка соединения до ответа — 502, не 200 с пустым потоком
    caplog.set_level(logging.INFO, logger="gateway")

    class SlowFail(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            await asyncio.sleep(0.05)  # задержка, чтобы длительность была реальной
            raise httpx.ConnectError("refused")

    def factory():
        return httpx.AsyncClient(
            transport=SlowFail(), base_url=NANO_BASE
        )

    monkeypatch.setattr(nanogpt, "_client", factory)
    r = _stream_chat()
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "upstream_unavailable"
    line = [l for l in caplog.messages if l.startswith("chat ")][-1]
    assert "duration_ms=0" not in line  # SPEC_006 §5: диагностика не подменена нулём


def test_stream_connect_timeout_before_body_returns_504(monkeypatch):
    calls = []

    def factory():
        calls.append(1)
        return httpx.AsyncClient(
            transport=BoomTransport(httpx.ConnectTimeout("slow")), base_url=NANO_BASE
        )

    monkeypatch.setattr(nanogpt, "_client", factory)
    r = _stream_chat()
    assert r.status_code == 504
    assert r.json()["error"]["code"] == "upstream_timeout"
    assert len(calls) == 1  # без повтора


# §4.4.5/§4.2.B: ошибка после начала передачи (реальный сокет:
# httpx.ASGITransport буферизует ответ upstream и не воспроизводит обрыв)


def test_stream_error_after_start_ends_without_fake_done(monkeypatch, caplog, tmp_path):
    caplog.set_level(logging.INFO, logger="gateway")
    up_port = _free_port()
    gw_port = _free_port()

    async def raw_upstream(reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        length = 0
        for line in head.decode(errors="ignore").split("\r\n"):
            if line.lower().startswith("content-length:"):
                length = int(line.split(":", 1)[1])
        if length:
            await reader.readexactly(length)
        # штатное начало + одно событие, затем обрыв заявленной длины (§4.2.B.6)
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"content-type: text/event-stream\r\n"
            b"content-length: 500\r\n\r\n"
            b'data: {"id": "chatcmpl-up-1", "object": "x"}\n\n'
        )
        await writer.drain()
        writer.close()

    def factory():
        return httpx.AsyncClient(base_url=f"http://127.0.0.1:{up_port}")

    async def go():
        up_server = await asyncio.start_server(raw_upstream, "127.0.0.1", up_port)
        gw_server, gw_task = await _start_server(gw.app, gw_port)
        try:
            status, got = None, []
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{gw_port}", timeout=10.0
            ) as client:
                async with client.stream(
                    "POST",
                    "/v1/chat/completions",
                    headers=AUTH,
                    json={
                        "model": TEST_MODEL,
                        "messages": [{"role": "user", "content": "hi"}],
                        "stream": True,
                    },
                ) as r:
                    status = r.status_code
                    async for line in r.aiter_lines():
                        if line.startswith("data: "):
                            got.append(line[6:])
            return status, got
        finally:
            up_server.close()
            await up_server.wait_closed()
            gw_server.should_exit = True
            await gw_task

    monkeypatch.setattr(nanogpt, "_client", factory)
    monkeypatch.setenv("NANOGPT_INFERENCE_BASE_URL", f"http://127.0.0.1:{up_port}")
    status, got = asyncio.run(go())
    assert status == 200  # статус уже отправлен, поменять нельзя
    assert got and got[0].startswith('{"id": "chatcmpl-up-1"')  # событие дошло
    assert "[DONE]" not in got  # поток обрывается честно, без подделки
    line = [l for l in caplog.messages if l.startswith("chat ")][-1]
    assert not line.rstrip().endswith("error=-")  # состояние зафиксировано в логе
    # SPEC_006 §1/§3: HTTP-статус первоначальный, результат потока отделён как ошибка
    assert "status=200" in line
    assert "upstream_status=200" in line
    assert "error=-" not in line
    # §5: длительность обрыва не подменена нулём
    assert "duration_ms=0" not in line
    # SPEC_008 §5 шаг 7.4: обрыв после HTTP 200 не маскируется под успех
    rec = json.loads((tmp_path / "request_log.jsonl").read_text().strip().splitlines()[-1])
    assert rec["status"] in {"unknown_outcome", "needs_reconciliation"}
    assert rec["usage_seen"] is False


def test_upstream_inflight_recorded_before_awaiting_upstream(fake, monkeypatch, tmp_path):
    # запись должна существовать до окончания генерации: иначе падение процесса
    # во время вызова upstream оставит запрос без следа (SPEC_008 §5 шаг 7)
    seen: list[str] = []
    real_run_chat = nanogpt.run_chat

    async def spy(*args, **kwargs):
        seen.extend((tmp_path / "request_log.jsonl").read_text().splitlines())
        return await real_run_chat(*args, **kwargs)

    monkeypatch.setattr(nanogpt, "run_chat", spy)
    r = _chat(model=TEST_MODEL, messages=[{"role": "user", "content": "hi"}])
    assert r.status_code == 200
    assert seen, "upstream_inflight отсутствовал до вызова upstream"
    assert json.loads(seen[-1])["status"] == "upstream_inflight"


def test_stream_normal_final_log_separates_states(fake, caplog, tmp_path):    # SPEC_006 §2/§3: штатный поток — успех отличим от ошибки, статусы разделены
    caplog.set_level(logging.DEBUG, logger="gateway")
    got = _first_stream_data()
    assert got[-1][1] == "[DONE]"
    # SPEC_008 §5 шаг 7: штатный стрим пишется как succeeded с upstream id
    rec = json.loads((tmp_path / "request_log.jsonl").read_text().strip().splitlines()[-1])
    assert rec["status"] == "succeeded"
    assert rec["upstream_request_id"] == "req_HEADER"
    assert rec["usage_seen"] is True
    info_lines = [l for l in caplog.messages if l.startswith(("chat ", "tokens "))]
    line = [l for l in caplog.messages if l.startswith("chat ")][-1]
    assert len(info_lines) == 2  # ровно summary + tokens
    assert "status=200" in line
    assert "upstream_status=200" in line
    assert "outcome=succeeded" in line
    assert line.rstrip().endswith("error=-")
    assert "tokens gateway_id=" in info_lines[1]
    assert "in=5 out=3" in info_lines[1]
    assert "cache_r=-" in info_lines[1]
    assert all("delta_keys" not in l and "events=" not in l for l in info_lines)
    # Диагностика разделяет время открытия upstream и задержку первого события/текста.
    debug_line = [l for l in caplog.messages if l.startswith("chat_debug ")][-1]
    assert "events=2" in debug_line
    assert "response_open_ms=" in debug_line
    assert "first_event_ms=" in debug_line
    assert "first_text_ms=" in debug_line
    assert "max_event_gap_ms=" in debug_line
    assert "first_tool_call_ms=" in debug_line
    assert "tool_call_events=0" in debug_line
    assert "tool_call_fragments=0" in debug_line
    assert "first_reasoning_ms=" in debug_line
    assert "reasoning_events=0" in debug_line
    assert "sse_event_types=message:2" in debug_line
    assert "delta_keys=content:2,role:1" in debug_line
    assert "finish_reasons=stop:1" in debug_line
    assert "upstream_fields=" in debug_line
    assert "error_detail=-" in debug_line


def test_unexpected_exception_after_start_logged_as_error(monkeypatch, caplog):
    # SPEC_006 §2: сбой не-типа UpstreamError после начала передачи
    # не выдаётся за штатное завершение, [DONE] не подделывается
    caplog.set_level(logging.INFO, logger="gateway")

    async def run_chat(*args, **kwargs):
        async def gen():
            yield json.dumps({"id": "c1", "object": "x"})
            raise ValueError("boom-internal")

        return gen()

    monkeypatch.setattr(nanogpt, "run_chat", run_chat)
    r = _stream_chat()
    assert r.status_code == 200  # заголовки уже отправлены, менять нельзя
    line = [l for l in caplog.messages if l.startswith("chat ")][-1]
    assert "error=ValueError" in line
    assert "error=-" not in line
    assert "[DONE]" not in r.text  # завершение не подделано


def test_malformed_upstream_response(fake):
    fake.mode = "malformed"
    r = _chat(model=TEST_MODEL, messages=[{"role": "user", "content": "hi"}])
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "upstream_protocol_error"


# §10.4 логи и секреты


def test_no_secrets_or_prompts_in_output(fake, caplog, capfd, tmp_path):
    caplog.set_level(logging.INFO, logger="gateway")
    capfd.readouterr()
    prompt = "TOP_SECRET_PROMPT_9f3"
    r = _chat(model=TEST_MODEL, messages=[{"role": "user", "content": prompt}])
    assert r.status_code == 200
    r2 = request("POST", "/v1/chat/completions", json={"model": TEST_MODEL, "messages": []})
    assert r2.status_code == 401
    fake.mode = "http_500"
    r3 = _chat(model=TEST_MODEL, messages=[{"role": "user", "content": prompt}])
    assert r3.status_code == 502
    captured = capfd.readouterr()
    log_text = (tmp_path / "request_log.jsonl").read_text()
    haystack = caplog.text + captured.out + captured.err + log_text
    assert prompt not in haystack
    assert GATEWAY_KEY not in haystack
    assert NANO_KEY not in haystack
    assert "authorization" not in haystack.lower() or "Bearer" not in haystack

# §10.3.16–17 неопределённые исходы


def test_sync_missing_usage_visible_not_free(fake, caplog):
    caplog.set_level(logging.INFO, logger="gateway")
    fake.mode = "sync_no_usage"
    r = _chat(model=TEST_MODEL, messages=[{"role": "user", "content": "hi"}])
    assert r.status_code == 200
    assert "usage" not in r.json()
    line = [l for l in caplog.messages if l.startswith("tokens ")][-1]
    assert "usage=missing" in line  # наблюдение, а не «бесплатный запрос»
    assert "cost=0.000001" in line  # cost из pricing известен даже без usage


def test_usage_base_url_not_used_by_runtime():
    # §10.2.10: настройка загружается конфигом, но код среза её не вызывает
    from pathlib import Path

    root = Path(gw.__file__).parent
    sources = [root / "main.py", root / "upstream" / "nanogpt.py", root / "upstream" / "stub.py"]
    for src in sources:
        assert "NANOGPT_USAGE_BASE_URL" not in src.read_text(encoding="utf-8"), src


# SPEC_009 §5: нормализация usage/pricing, две INFO-строки, JSONL enrichment


def test_extract_token_usage_usage_priority_and_details():
    body = {
        "usage": {
            "prompt_tokens": 1200,
            "completion_tokens": 340,
            "total_tokens": 1540,
            "reasoning_tokens": 40,
            "cached_tokens": 50,
            "cache_write_tokens": 7,
            "cost": 0.0012,
            "currency": "USD",
        },
        "x_nanogpt_pricing": {
            "amount": 9.99,
            "currency": "EUR",
            "inputTokens": 1,
            "outputTokens": 1,
            "cacheReadInputTokens": 1,
            "requestId": "req_1",
        },
    }
    usage = nanogpt.extract_token_usage(body)
    assert (usage.input, usage.output, usage.total) == (1200, 340, 1540)
    assert usage.cache_read == 50
    assert usage.cache_write == 7
    assert usage.reasoning == 40
    # приоритет: usage.cost выше pricing.amount
    assert usage.upstream_cost == "0.0012"
    assert usage.currency == "USD"
    assert usage.cost_seen is True
    assert usage.usage_seen is True


def test_extract_token_usage_pricing_only_and_usd_fallback():
    pricing_only = {
        "x_nanogpt_pricing": {
            "inputTokens": 10,
            "outputTokens": 4,
            "cacheReadInputTokens": 2,
            "amount": 0.02,
            "currency": "EUR",
        }
    }
    usage = nanogpt.extract_token_usage(pricing_only)
    assert (usage.input, usage.output, usage.total) == (10, 4, 14)
    assert usage.cache_read == 2
    assert usage.upstream_cost == "0.02"
    assert usage.currency == "EUR"
    assert usage.usage_seen is False

    usd = nanogpt.extract_token_usage({"x_nanogpt_pricing": {"costUsd": 0.03}})
    assert usd.upstream_cost == "0.03"
    assert usd.currency == "USD"


def test_extract_token_usage_empty_is_missing_not_zero():
    usage = nanogpt.extract_token_usage({})
    assert usage.usage_seen is False
    assert usage.cost_seen is False
    assert usage.upstream_cost is None
    assert all(
        getattr(usage, name) is None
        for name in ("input", "output", "cache_read", "cache_write", "reasoning", "total")
    )


def test_extract_token_usage_does_not_invent_total():
    usage = nanogpt.extract_token_usage({"usage": {"prompt_tokens": 5}})
    assert usage.input == 5
    assert usage.output is None
    assert usage.total is None


def test_derived_metrics_and_message_stats():
    usage = nanogpt.TokenUsage(input=200, output=100, cache_read=50)
    tps, hit = gw._derived_metrics(usage, 2000)
    assert tps == 50.0
    assert hit == 0.25
    assert gw._derived_metrics(usage, 0) == (None, 0.25)
    assert gw._derived_metrics(nanogpt.TokenUsage(input=0, cache_read=0), 1000) == (None, None)

    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": [{"type": "text", "text": "ignored"}]},
        {"role": "user", "content": "hi"},
    ]
    count, chars, roles = gw._message_stats(messages)
    assert count == 4
    assert chars == 8  # только str content, без текста вложений
    assert roles == "system:1,user:2,assistant:1"


def test_info_has_two_lines_and_debug_is_separate(fake, caplog):
    caplog.set_level(logging.INFO, logger="gateway")
    r = _chat(
        model=TEST_MODEL,
        messages=[{"role": "user", "content": "hello"}],
        stream=False,
    )
    assert r.status_code == 200
    lines = [l for l in caplog.messages if l.startswith(("chat ", "tokens "))]
    assert len(lines) == 2
    assert lines[0].startswith("chat gateway_id=")
    assert lines[1].startswith("tokens gateway_id=")
    gateway_id = r.headers["x-request-id"]
    assert f"gateway_id={gateway_id}" in lines[0]
    assert f"gateway_id={gateway_id}" in lines[1]
    assert "in=5 out=3" in lines[1]
    assert "cost=0.000001 currency=USD cost_seen=1 usage_seen=1" in lines[1]
    # SSE-жир не попадает в INFO
    assert "delta_keys" not in caplog.text
    assert "sse_event_types" not in caplog.text
    assert "events=" not in caplog.text


def test_info_debug_retains_sse_diagnostics(fake, caplog):
    caplog.set_level(logging.DEBUG, logger="gateway")
    r = _chat(
        model=TEST_MODEL,
        messages=[{"role": "user", "content": "hello"}],
        stream=False,
    )
    assert r.status_code == 200
    debug_line = [l for l in caplog.messages if l.startswith("chat_debug ")][-1]
    assert "upstream_fields=" in debug_line
    assert "extra_fields=" in debug_line
    assert "error_detail=" in debug_line


def test_request_log_enriched_tokens_cost_but_no_prompt(fake, tmp_path):
    prompt = "TOP_SECRET_PROMPT_SPEC009"
    r = _chat(
        model=TEST_MODEL,
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": "ok"},
        ],
    )
    assert r.status_code == 200
    rec = json.loads((tmp_path / "request_log.jsonl").read_text().strip().splitlines()[-1])
    assert rec["status"] == "succeeded"
    assert rec["input"] == 5
    assert rec["output"] == 3
    assert rec["total"] == 8
    assert rec["upstream_cost"] == "0.000001"
    assert rec["currency"] == "USD"
    assert rec["cost_seen"] is True
    assert rec["usage_seen"] is True
    assert rec["messages_count"] == 3
    assert rec["messages_chars"] == 3 + len(prompt) + 2
    assert rec["roles"] == "system:1,user:1,assistant:1"
    assert "ttft_ms" in rec  # sync без SSE-событий может быть null
    assert "duration_ms" in rec
    assert prompt not in json.dumps(rec)
    assert "x_nanogpt_pricing" not in json.dumps(rec)


def test_missing_usage_and_cost_unknown_explicit(fake, caplog, tmp_path):
    caplog.set_level(logging.INFO, logger="gateway")
    fake.mode = "sync_nothing"
    r = _chat(model=TEST_MODEL, messages=[{"role": "user", "content": "hi"}])
    assert r.status_code == 200
    line = [l for l in caplog.messages if l.startswith("tokens ")][-1]
    assert "usage=missing" in line
    assert "cost=unknown" in line
    assert "cost_seen=0" in line
    rec = json.loads((tmp_path / "request_log.jsonl").read_text().strip().splitlines()[-1])
    assert rec["usage_seen"] is False
    assert rec["cost_seen"] is False
    assert rec["upstream_cost"] is None
    assert rec["input"] is None
