import asyncio
import json
import logging

import httpx
import pytest

from app.main import app

TEST_KEY = "test-gateway-key-123"
AUTH = {"Authorization": f"Bearer {TEST_KEY}"}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("GATEWAY_TEST_API_KEY", TEST_KEY)


def request(method, path, **kwargs):
    async def _do():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(_do())


def _chat(**payload):
    return request("POST", "/v1/chat/completions", headers=AUTH, json=payload)


# §9.1


def test_health():
    assert request("GET", "/health").status_code == 200


def test_models_without_auth():
    assert request("GET", "/v1/models").status_code == 401


def test_models_wrong_key():
    r = request("GET", "/v1/models", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_api_key"


def test_models_ok():
    r = request("GET", "/v1/models", headers=AUTH)
    assert r.status_code == 200
    data = r.json()
    assert data["object"] == "list"
    assert any(m["id"] == "01" for m in data["data"])


def test_unknown_model():
    r = _chat(model="xyz", messages=[{"role": "user", "content": "hi"}])
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "model_not_found"
    assert r.json()["error"]["message"] == "The model `xyz` does not exist"


def test_sync_completion():
    r = _chat(model="01", messages=[{"role": "user", "content": "привет"}])
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == "01"
    assert "[local-stub]" in body["choices"][0]["message"]["content"]
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"]["prompt_tokens"] > 0
    assert body["usage"]["completion_tokens"] > 0
    assert body["usage"]["total_tokens"] == (
        body["usage"]["prompt_tokens"] + body["usage"]["completion_tokens"]
    )


def test_stream():
    r = _chat(
        model="01",
        messages=[{"role": "user", "content": "hi"}],
        stream=True,
        stream_options={"include_usage": True},
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    events = [line[6:] for line in r.text.splitlines() if line.startswith("data: ")]
    assert events[-1] == "[DONE]"
    chunks = [json.loads(e) for e in events[:-1]]
    assert all(c["object"] == "chat.completion.chunk" for c in chunks)
    contents = [
        c["choices"][0]["delta"].get("content", "")
        for c in chunks
        if c.get("choices") and c["choices"][0].get("finish_reason") is None
    ]
    assert any(contents)
    assert "[local-stub]" in "".join(contents)
    finish = [
        c for c in chunks if c.get("choices") and c["choices"][0].get("finish_reason") == "stop"
    ]
    assert finish
    usage_chunks = [c for c in chunks if not c.get("choices") and "usage" in c]
    assert len(usage_chunks) == 1
    assert usage_chunks[0]["usage"]["total_tokens"] > 0
    assert all(c["id"] == chunks[0]["id"] for c in chunks)


def test_key_not_in_logs_or_responses(caplog):
    caplog.set_level(logging.INFO, logger="gateway")
    r = _chat(model="01", messages=[{"role": "user", "content": "hi"}])
    assert r.status_code == 200
    assert TEST_KEY not in r.text
    r2 = request("GET", "/v1/models", headers=AUTH)
    assert TEST_KEY not in r2.text
    assert TEST_KEY not in caplog.text


# поведение из §4.1/§4.2/§4.4, не покрытое минимальным набором выше


def test_chat_requires_auth():
    r = request(
        "POST",
        "/v1/chat/completions",
        json={"model": "01", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 401


def test_invalid_json():
    r = request(
        "POST",
        "/v1/chat/completions",
        headers={**AUTH, "Content-Type": "application/json"},
        content=b"{not json",
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_json"


def test_missing_messages():
    r = _chat(model="01")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_messages"


def test_body_too_large():
    r = _chat(model="01", messages=[{"role": "user", "content": "x" * (1_048_576 + 1)}])
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "body_too_large"
