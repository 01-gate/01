import asyncio
import json
import logging

import httpx
import pytest

from app import config, limits
from app.main import app
from app.models_catalog import MODEL_CATALOG

TEST_KEY = "test-gateway-key-123"
TEST_MODEL = config.test_model()
AUTH = {"Authorization": f"Bearer {TEST_KEY}"}


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("GATEWAY_TEST_API_KEY", TEST_KEY)
    # этот файл проверяет legacy stub-режим (SPEC_004 §2.11)
    monkeypatch.setenv("GATEWAY_UPSTREAM", "stub")
    monkeypatch.setenv("GATEWAY_REQUEST_LOG_PATH", str(tmp_path / "request_log.jsonl"))
    limits.reset()
    yield
    limits.reset()


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
    response = request("GET", "/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_authenticated_health_reports_upstream_mode(monkeypatch):
    monkeypatch.setenv("GATEWAY_UPSTREAM", "stub")
    response = request("GET", "/health", headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "upstream_mode": "stub"}


def test_unauthenticated_health_does_not_disclose_upstream_mode():
    response = request("GET", "/health")
    assert response.status_code == 200
    assert "upstream_mode" not in response.json()


def test_models_without_auth():
    # публичный endpoint без авторизации (SPEC_004 §4.1)
    r = request("GET", "/v1/models")
    assert r.status_code == 200
    assert r.json()["object"] == "list"


def test_models_arbitrary_key_still_public():
    r = request("GET", "/v1/models", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 200
    assert any(m["id"] == TEST_MODEL for m in r.json()["data"])


def test_models_ok():
    r = request("GET", "/v1/models", headers=AUTH)
    assert r.status_code == 200
    data = r.json()
    assert data["object"] == "list"
    assert any(m["id"] == TEST_MODEL for m in data["data"])


def test_catalog_is_single_source_of_truth():
    # SPEC_008 §5 шаг 1: GET /v1/models и accept-list chat — одно множество ID
    r = request("GET", "/v1/models")
    assert [m["id"] for m in r.json()["data"]] == [m["id"] for m in MODEL_CATALOG]


def test_old_public_model_01_rejected():
    # публичная модель 01 не восстанавливается (SPEC_008 §2)
    r = _chat(model="01", messages=[{"role": "user", "content": "hi"}])
    assert r.status_code == 404


def test_unknown_model():
    r = _chat(model="xyz", messages=[{"role": "user", "content": "hi"}])
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "model_not_found"
    assert r.json()["error"]["message"] == "The model `xyz` does not exist"


def test_gateway_request_id_header_stable_and_in_logs(caplog):
    caplog.set_level(logging.INFO, logger="gateway")
    r1 = _chat(model=TEST_MODEL, messages=[{"role": "user", "content": "hi"}])
    rid = r1.headers["x-request-id"]
    assert rid
    assert f"gateway_id={rid}" in caplog.text
    caplog.clear()
    r2 = _chat(model=TEST_MODEL, messages=[{"role": "user", "content": "hi"}])
    assert r2.headers["x-request-id"] != rid
    assert f"gateway_id={r2.headers['x-request-id']}" in caplog.text


def test_error_responses_carry_gateway_request_id(caplog):
    # клиент сопоставляет любой ответ (включая ошибку) с логом по X-Request-ID
    caplog.set_level(logging.INFO, logger="gateway")

    unauthorized = request(
        "POST", "/v1/chat/completions", json={"model": TEST_MODEL, "messages": [{"role": "user"}]}
    )
    assert unauthorized.status_code == 401
    rid = unauthorized.headers["x-request-id"]
    assert f"gateway_request_id={rid}" in caplog.text

    not_found = _chat(model="xyz", messages=[{"role": "user", "content": "hi"}])
    assert not_found.status_code == 404
    assert not_found.headers["x-request-id"]


def test_stream_response_carries_gateway_request_id():
    r = _chat(model=TEST_MODEL, messages=[{"role": "user", "content": "hi"}], stream=True)
    assert r.status_code == 200
    assert r.headers["x-request-id"]


def test_request_log_records_succeeded(tmp_path):
    r = _chat(model=TEST_MODEL, messages=[{"role": "user", "content": "hi"}])
    assert r.status_code == 200
    rec = json.loads((tmp_path / "request_log.jsonl").read_text().strip().splitlines()[-1])
    assert rec["status"] == "succeeded"
    assert rec["gateway_request_id"] == r.headers["x-request-id"]
    assert rec["model"] == TEST_MODEL


def test_rate_limit_returns_429_before_upstream(monkeypatch):
    monkeypatch.setenv("GATEWAY_MAX_CONCURRENT_CHAT", "1")
    assert limits.try_acquire() is True  # занимаем единственный слот
    try:
        r = _chat(model=TEST_MODEL, messages=[{"role": "user", "content": "hi"}])
    finally:
        limits.release()
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "rate_limit_exceeded"
    assert r.headers["x-request-id"]


def test_sync_completion():
    r = _chat(model=TEST_MODEL, messages=[{"role": "user", "content": "привет"}])
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == TEST_MODEL
    assert "[local-stub]" in body["choices"][0]["message"]["content"]
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"]["prompt_tokens"] > 0
    assert body["usage"]["completion_tokens"] > 0
    assert body["usage"]["total_tokens"] == (
        body["usage"]["prompt_tokens"] + body["usage"]["completion_tokens"]
    )


def test_stream():
    r = _chat(
        model=TEST_MODEL,
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
    r = _chat(model=TEST_MODEL, messages=[{"role": "user", "content": "hi"}])
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
        json={"model": TEST_MODEL, "messages": [{"role": "user", "content": "hi"}]},
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
    r = _chat(model=TEST_MODEL)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_messages"


def test_body_too_large():
    r = _chat(model=TEST_MODEL, messages=[{"role": "user", "content": "x" * (1_048_576 + 1)}])
    assert r.status_code == 413
    assert r.headers["x-request-id"]
    assert r.json()["error"]["code"] == "body_too_large"
