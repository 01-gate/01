# OpenCode request inspector

Локальный FastAPI-приёмник для наблюдения за реальными HTTP-запросами OpenCode (`@ai-sdk/openai-compatible`). Генерации нет, upstream нет, секреты в лог не попадают.

## Запуск

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## Подключение OpenCode

`opencode.json` в корне объявляет провайдер `local` с моделью `01`. Выбери `local/01` в OpenCode и отправь запрос. Топ-level `"model"` не задан намеренно: конфиг не перехватывает модель по умолчанию.

## Фактический контракт

- `POST /api`, `POST /api/`, `POST /api/{path}` — приём JSON, pretty-print в stdout.
- Ответ — диагностический JSON, **не** OpenAI Chat Completions: `{"received", "path", "json_valid"}`.
- Некорректный JSON — `400`, тело больше 1 MiB — `413`. Ошибки не роняют сервер.
- `GET /health` — `{"status":"ok"}`.
- `GET /v1/models` — каталог из одной модели `01` (захардкожен).
- `GET /api` — `405`. Другие методы не реализованы намеренно.
- Не логируются: `Authorization`, `Cookie`, `Set-Cookie`, `X-Api-Key`; в JSON значения ключей `api_key`, `token`, `secret`, `password` и т.п.

## Ограничения совместимости

Диагностический ответ не содержит `choices`/`message`/`finish_reason`, поэтому запрос OpenCode завершится ошибкой клиента. Это ожидаемо: этап фиксирует входящий контракт. Следующий этап — минимальный ответ Chat Completions.

## Тесты

```bash
pytest
```
