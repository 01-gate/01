"""Конфигурация приложения из проектного .env (SPEC_004 §3).

Обязательные настройки проверяются при старте; значения секретов
не логируются и не попадают в текст ошибок.
"""

from __future__ import annotations

import os
from pathlib import Path

_PREFIXES = ("NANOGPT_", "GATEWAY_")
_REQUIRED = ("NANOGPT_INFERENCE_BASE_URL", "NANOGPT_API_KEY")


class ConfigError(RuntimeError):
    """Отсутствуют обязательные настройки (без значений в сообщении)."""


def default_env_path() -> Path:
    # путь от расположения кода, не от cwd (SPEC_005 §3.2.1)
    return Path(__file__).resolve().parent.parent / ".env"


def _load_dotenv(path: str | Path = ".env") -> None:
    # shortcut: минимальный парсер вместо python-dotenv; setdefault, не перезаписывает env
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.partition("=")
            key = key.strip()
            if sep and key.startswith(_PREFIXES):
                os.environ.setdefault(key, value.strip().strip("\"'"))


def test_model() -> str:
    """Публичный ID модели для тестов и smoke-скриптов.

    Переопределяется `GATEWAY_TEST_MODEL`, чтобы можно было проверить другую
    модель без правки кода.
    """
    return os.environ.get("GATEWAY_TEST_MODEL") or "deepseek"


def load(path: str | Path | None = None) -> None:
    """Загружает .env, валидирует обязательные настройки.

    Путь по умолчанию — `.env` в корне проекта (рядом с пакетом `app`),
    независимо от текущей рабочей директории.

    Политика конфликтов (SPEC_005 §3.2, единое правило): переменные
    окружения имеют приоритет над `.env`; файл заполняет только
    отсутствующие в окружении ключи. Значения секретов не печатаются.
    """
    _load_dotenv(default_env_path() if path is None else path)
    missing = [name for name in _REQUIRED if not os.environ.get(name)]
    if missing:
        raise ConfigError(
            "Missing required configuration: " + ", ".join(missing)
        )