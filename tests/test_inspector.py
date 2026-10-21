import logging

import pytest
from fastapi.testclient import TestClient

from app.main import MAX_BODY_BYTES, app

client = TestClient(app)


def test_api_accepts_valid_json(caplog):
    caplog.set_level(logging.INFO, logger="inspector")
    r = client.post("/api", json={"hello": "world"})
    assert r.status_code == 200
    assert r.json() == {"received": True, "path": "/api", "json_valid": True}


def test_chat_completions_path_accepts_valid_json():
    r = client.post("/api/chat/completions", json={"model": "01", "messages": []})
    assert r.status_code == 200
    assert r.json() == {"received": True, "path": "/api/chat/completions", "json_valid": True}


def test_nested_structure_preserved_in_log(caplog):
    caplog.set_level(logging.INFO, logger="inspector")
    payload = {"a": {"b": [1, 2, {"c": None, "d": True}]}, "e": 1.5}
    r = client.post("/api", json=payload)
    assert r.status_code == 200
    assert '"c": null' in caplog.text
    assert '"d": true' in caplog.text
    assert '"e": 1.5' in caplog.text


def test_missing_model_and_messages_is_fine():
    r = client.post("/api/chat/completions", json={"temperature": 0.5})
    assert r.status_code == 200
    assert r.json()["json_valid"] is True


def test_invalid_json_is_controlled():
    r = client.post("/api", content=b"{not json", headers={"content-type": "application/json"})
    assert r.status_code == 400
    assert r.json() == {"received": True, "path": "/api", "json_valid": False}


def test_empty_body_is_controlled():
    r = client.post("/api", content=b"")
    assert r.status_code == 400
    assert r.json()["json_valid"] is False


def test_non_json_content_type_still_parsed_if_json():
    r = client.post("/api", content=b'{"x": 1}', headers={"content-type": "text/plain"})
    assert r.status_code == 200
    assert r.json()["json_valid"] is True


def test_secret_headers_not_logged(caplog):
    caplog.set_level(logging.INFO, logger="inspector")
    r = client.post(
        "/api",
        json={"ok": 1},
        headers={"Authorization": "Bearer super-secret-token", "Cookie": "sid=1", "X-Api-Key": "sk-leak"},
    )
    assert r.status_code == 200
    assert "super-secret-token" not in caplog.text
    assert "sid=1" not in caplog.text
    assert "sk-leak" not in caplog.text
    assert "<redacted>" in caplog.text


def test_secret_values_in_json_redacted(caplog):
    caplog.set_level(logging.INFO, logger="inspector")
    r = client.post("/api", json={"api_key": "sk-should-not-appear", "model": "01"})
    assert r.status_code == 200
    assert "sk-should-not-appear" not in caplog.text


def test_oversized_body_rejected():
    r = client.post("/api", content=b"x" * (MAX_BODY_BYTES + 1))
    assert r.status_code == 413
    assert r.json() == {"received": False, "path": "/api", "error": "body_too_large"}


def test_error_does_not_stop_server():
    assert client.post("/api", content=b"{oops").status_code == 400
    assert client.post("/api", content=b"{oops").status_code == 400
    r = client.post("/api", json={"still": "alive"})
    assert r.status_code == 200
    assert r.json()["json_valid"] is True


def test_get_not_implemented():
    # Не имитируем поддержку GET: честный 405 по умолчанию
    assert client.get("/api").status_code == 405


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_v1_models():
    r = client.get("/v1/models")
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    (model,) = body["data"]
    assert model["id"] == "01"
    assert model["object"] == "model"
    assert model["owned_by"] == "local"
    assert isinstance(model["created"], int) and model["created"] > 0
