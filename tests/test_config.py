"""SPEC_004 §10.2: конфигурация из .env, обязательные настройки, fail-fast."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from app import config

SANDBOX = Path(__file__).resolve().parent.parent

REQUIRED = ("NANOGPT_INFERENCE_BASE_URL", "NANOGPT_API_KEY")


def test_dotenv_loaded_automatically(tmp_path, monkeypatch):
    for name in REQUIRED:
        monkeypatch.delenv(name, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        'NANOGPT_INFERENCE_BASE_URL="https://example.test/v1"\n'
        "NANOGPT_API_KEY=secret-loadcheck\n"
        "GATEWAY_TEST_API_KEY=gw-loadcheck\n"
        "# комментарий\n"
        "UNRELATED=value\n"
    )
    config.load(str(env_file))
    assert os.environ["NANOGPT_INFERENCE_BASE_URL"] == "https://example.test/v1"
    assert os.environ["NANOGPT_API_KEY"] == "secret-loadcheck"
    assert "UNRELATED" not in os.environ


def test_missing_required_config_names_secret_not_leaked(tmp_path, monkeypatch):
    for name in REQUIRED:
        monkeypatch.delenv(name, raising=False)
    empty = tmp_path / ".env"
    empty.write_text("GATEWAY_TEST_API_KEY=abc\n")
    with pytest.raises(config.ConfigError) as excinfo:
        config.load(str(empty))
    message = str(excinfo.value)
    assert "NANOGPT_INFERENCE_BASE_URL" in message
    assert "NANOGPT_API_KEY" in message
    assert "abc" not in message  # значения секретов в ошибках нет


def test_app_starts_fail_fast_without_config(tmp_path):
    # §3.2: приложение без обязательных настроек завершается с понятной ошибкой
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(SANDBOX)}
    proc = subprocess.run(
        [sys.executable, "-c", "import app.main"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 2
    assert "Missing required configuration" in proc.stderr
    assert "NANOGPT_API_KEY" in proc.stderr