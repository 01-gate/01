"""Конфигурация приложения из проектного .env (SPEC_004 §3).

Обязательные настройки проверяются при старте; значения секретов
не логируются и не попадают в текст ошибок.
"""

from __future__ import annotations

import os

_PREFIXES = ("NANOGPT_", "GATEWAY_")
_REQUIRED = ("NANOGPT_INFERENCE_BASE_URL", "NANOGPT_API_KEY")


class ConfigError(RuntimeError):
    """Отсутствуют обязательные настройки (без значений в сообщении)."""


def _load_dotenv(path: str = ".env") -> None:
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


def load(path: str = ".env") -> None:
    """Загружает .env и валидирует обязательные настройки."""
    _load_dotenv(path)
    missing = [name for name in _REQUIRED if not os.environ.get(name)]
    if missing:
        raise ConfigError(
            "Missing required configuration: " + ", ".join(missing)
        )