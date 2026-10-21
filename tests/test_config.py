"""SPEC_004 §10.2 и SPEC_005 §3.3: конфигурация, fail-fast, изоляция от cwd."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app import config

SANDBOX = Path(__file__).resolve().parent.parent

REQUIRED = ("NANOGPT_INFERENCE_BASE_URL", "NANOGPT_API_KEY")


def _mini_root(tmp_path: Path, env_text: str | None = None) -> Path:
    # изолированный корень: копия пакета app + свой .env; настоящие
    # пользовательские секреты тестами не читаются (SPEC_005 §3.3)
    root = tmp_path / "root"
    shutil.copytree(SANDBOX / "app", root / "app")
    if env_text is not None:
        (root / ".env").write_text(env_text, encoding="utf-8")
    return root


def _run(root: Path, cwd: Path, code: str, extra_env: dict | None = None):
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(root)}
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


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


def test_default_path_is_project_root_not_cwd(tmp_path):
    assert config.default_env_path() == (
        Path(config.__file__).resolve().parent.parent / ".env"
    )


def test_loads_project_env_when_cwd_is_elsewhere(tmp_path):
    # §3.3/§3.4: запуск из чужой директории загружает проектный .env
    root = _mini_root(
        tmp_path,
        "NANOGPT_INFERENCE_BASE_URL=https://example.test/v1\n"
        "NANOGPT_API_KEY=secret-cwdcheck\n",
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    proc = _run(
        root,
        elsewhere,
        "import os, app.config as c; c.load();"
        "print(os.environ['NANOGPT_INFERENCE_BASE_URL'])",
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "https://example.test/v1"
    assert "secret-cwdcheck" not in proc.stderr  # секреты не в диагностике


def test_env_overrides_dotenv_documented_policy(tmp_path):
    # единая политика конфликтов (SPEC_005 §3.2.3): окружение приоритетнее .env
    root = _mini_root(
        tmp_path,
        "NANOGPT_INFERENCE_BASE_URL=https://from-file.test/v1\n"
        "NANOGPT_API_KEY=from-file\n",
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    proc = _run(
        root,
        elsewhere,
        "import os, app.config as c; c.load();"
        "print(os.environ['NANOGPT_INFERENCE_BASE_URL'])",
        extra_env={"NANOGPT_INFERENCE_BASE_URL": "https://from-env.test/v1"},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "https://from-env.test/v1"


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


def test_missing_env_file_is_predictable(tmp_path):
    # §3.3: нет файла — та же понятная ошибка, без чтения чужого .env
    root = _mini_root(tmp_path)  # корень без .env
    proc = _run(root, tmp_path, "import app.config; app.config.load()")
    assert proc.returncode != 0
    assert "Missing required configuration" in proc.stderr


def test_app_starts_fail_fast_without_config(tmp_path):
    # §3.2: без обязательных настроек завершается с понятной ошибкой,
    # независимо от рабочей директории
    root = _mini_root(tmp_path)  # корень без .env
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    proc = _run(root, elsewhere, "import app.main")
    assert proc.returncode == 2
    assert "Missing required configuration" in proc.stderr
    assert "NANOGPT_API_KEY" in proc.stderr