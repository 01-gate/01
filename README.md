# zer0ne local gateway (NanoGPT)

Локальный OpenAI-compatible gateway для OpenCode: авторизация клиентским ключом,
публичный каталог разрешённых моделей, Chat Completions (sync и SSE) через реальный
upstream **NanoGPT** (см. `.spec/SPEC_004.md`).

## Запуск

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # заполнить ключи
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Все настройки читаются из проектного `.env` автоматически, ручные `export` не нужны.
Без обязательных `NANOGPT_INFERENCE_BASE_URL` / `NANOGPT_API_KEY` приложение завершается
с понятной ошибкой (без вывода значений). `HOST`/`PORT`/`LOG_LEVEL` — опционально
(по умолчанию `127.0.0.1` / `8000` / `INFO`). Без `GATEWAY_TEST_API_KEY` сервер
стартует с warning и отвечает 401 на защищённые маршруты.

## Контракт

- `GET /health` — liveness, без auth.
- `GET /v1/models` — публичный каталог, **авторизации не требует** (решение §2.4/§4.1).
  Отдаётся локальный allowlist (`01`), весь каталог NanoGPT наружу не раскрывается.
- `POST /v1/chat/completions` — `Authorization: Bearer <gateway-key>` обязателен,
  sync (`stream: false`) или SSE (`stream: true`).
  - Публичный `model` маппится в точный upstream ID; клиентский ключ в NanoGPT не передаётся.
  - Неверный/отсутствующий ключ → `401` **до** любого вызова upstream.
  - Неизвестная модель → `404 model_not_found` без вызова upstream.
  - Невалидный JSON / нет `messages` → `400`; тело > 1 MiB → `413`.
  - Ошибки upstream: `502` (connect/HTTP/protocol), `504` (timeout); повторов нет.
  - Внутренняя себестоимость (`x_nanogpt_pricing`) в публичном ответе скрывается,
    `usage` сохраняется; идентификаторы `id`, `X-Request-ID` и billing lookup ID
    не смешиваются.
  - Пути `/api/v1` не обслуживаются; Usage/Request Billing в этом срезе не вызываются.
- Режим `GATEWAY_UPSTREAM=stub` в `.env` переключает на старую stub-генерацию
  (для тестов/разработки без сети).

## Логи

В логи попадают только метаданные: route, статусы, модели, ID, длительность.
`messages`, chunks, `Authorization` и содержимое `.env` не логируются.

## OpenCode

`opencode.jsonc`, провайдер `local`: `baseURL: http://127.0.0.1:8000/v1`,
`apiKey` — значение клиентского ключа gateway (не ключ NanoGPT), модель `01`.
Это значение должно совпадать с `GATEWAY_TEST_API_KEY` в проектном `.env`
(спека §9: глобальную конфигурацию OpenCode на этом срезе не меняем).

## Тесты

```bash
pytest
```