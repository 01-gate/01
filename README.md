# zer0ne local gateway (mock)

Локальный OpenAI-compatible API для OpenCode: auth по тестовому ключу, каталог моделей, Chat Completions (sync и SSE stream) со **stub-генерацией**. Внешних HTTP-вызовов нет — NanoGPT не подключён (см. `.spec/SPEC_002.md`).

## Запуск

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
set -a; . ./.env; set +a
export GATEWAY_TEST_API_KEY
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

`HOST`/`PORT`/`LOG_LEVEL` — опционально (по умолчанию `127.0.0.1` / `8000` / `INFO`). Без `GATEWAY_TEST_API_KEY` сервер стартует с warning и отвечает 401 на защищённые маршруты.

## Контракт

- `GET /health` — liveness, без auth.
- `GET /v1/models` — каталог (модель `01`), без auth.
- `POST /v1/chat/completions` — Bearer обязателен, sync (`stream: false`) или SSE (`stream: true`).
  - stream: чанки по словам, финальный `finish_reason: "stop"`, при `stream_options.include_usage: true` — отдельный usage-чанок, затем `data: [DONE]`.
- Неверный/отсутствующий ключ → `401` **до** генерации. Невалидный JSON / нет `messages` → `400`. Неизвестная модель → `404 model_not_found`. Тело > 1 MiB → `413`. Пути `/api/v1` не обслуживаются.
- Форма ответов — OpenAI Chat Completions; текст заглушки: `[local-stub] model=01 ok. Last user: ...`; usage — детерминированная фейковая оценка (`len/4`), не биллинг.
- В логи не попадают ключ и полные `messages` (только метаданные).

## OpenCode

`opencode.jsonc`, провайдер `local`: `baseURL: http://127.0.0.1:8000/v1`, `apiKey: {env:GATEWAY_TEST_API_KEY}`, модель `01`. Значение `GATEWAY_TEST_API_KEY` в окружении OpenCode должно совпадать с серверным.

## Тесты

```bash
pytest
```
