# zer0ne local gateway (NanoGPT)

Локальный OpenAI-compatible gateway для OpenCode. Текущий рабочий срез проксирует Chat Completions через реальный NanoGPT: sync и SSE, с локальным ключом клиента и собственным allowlist моделей.

## Что поддерживается

- `GET /health` — публичная проверка доступности; с валидным gateway key дополнительно возвращает `upstream_mode` для безопасного preflight диагностики.
- `GET /v1/models` — публичный каталог разрешённых gateway моделей, без авторизации.
- `POST /v1/chat/completions` — обязательный Bearer key gateway; `stream: false` и `stream: true`.
- Публичный каталог задаёт единый источник правды `app/models_catalog.py`: `GET /v1/models` и accept-list chat используют одно и то же множество ID. Текущий каталог: `mimo`, `deepseek`, `deepseek-last`, `glm`, `spark`, `qwen`. Клиент передаёт публичный ID (например `deepseek`), а не upstream ID.
- Каждая обработка chat получает gateway correlation ID и возвращает его в заголовке `X-Request-ID`. Upstream request ID (если есть) возвращается отдельно в `X-Upstream-Request-ID` и не подменяет gateway ID.
- OpenAI-compatible tool calling: `tools`, `tool_choice`, `parallel_tool_calls`, сообщения с `tool_calls` и `role: "tool"`.
- Поток передаётся без полной буферизации. При нормальном завершении gateway выдаёт один `[DONE]`; ошибка после начала ответа не превращается в успешное завершение.
- Usage сохраняется в ответе, если upstream его возвращает. Внутреннее поле `x_nanogpt_pricing` в клиентский ответ не передаётся.
- Простые in-process лимиты: max concurrent chat-запросов и optional окно частоты. Превышение возвращает OpenAI-compatible `429 rate_limit_exceeded` до вызова upstream.
- Автоматических повторов генерации нет. Если соединение оборвалось после начала запроса, исход может остаться неизвестным; smoke test не запускает повтор автоматически.
- Каждый chat-запрос получает статус (`upstream_inflight` — до ожидания upstream, далее `succeeded`, `upstream_error`, `unknown_outcome`, `client_disconnected`, `needs_reconciliation`) и пишется строками JSONL в `data/request_log.jsonl` (путь переопределяется `GATEWAY_REQUEST_LOG_PATH`). Строка `upstream_inflight` пишется до вызова NanoGPT, поэтому авария процесса во время генерации не оставляет запрос без следа. В файл попадают только id, модель, статус, `usage_seen` и timestamps — не секреты и не содержимое запроса.

В этом срезе нет биллинга gateway, PostgreSQL, балансов, резервов, Usage API или Request Billing API.

## 1. Установка с нуля

Требуется Python 3.10 или новее.

```bash
git clone https://github.com/01-gate/01.git
cd 01
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Открой проектный `.env` и заполни:

- `NANOGPT_API_KEY` — inference API key NanoGPT;
- `GATEWAY_TEST_API_KEY` — локальный ключ, которым OpenCode авторизуется на gateway;
- `GATEWAY_UPSTREAM=nanogpt` — включает реальный upstream;
- `NANOGPT_INFERENCE_BASE_URL=https://api.nano-gpt.com/v1` — текущая конфигурация inference API.

Необязательные значения с безопасными умолчаниями (см. `.env.example`): `GATEWAY_MAX_CONCURRENT_CHAT` (default `8`), `GATEWAY_RATE_LIMIT_REQUESTS` (default `0`, лимит выключен) и `GATEWAY_RATE_LIMIT_WINDOW_SECONDS` (default `60`), `GATEWAY_REQUEST_LOG_PATH` (default `data/request_log.jsonl`), `GATEWAY_TEST_MODEL` (default `deepseek`) — публичная модель для тестов и smoke/compare скриптов.

Не добавляй кавычки вокруг значений без необходимости. Приложение само загружает проектный `.env`; вручную выполнять `export` не нужно. Путь к файлу определяется относительно расположения кода, а не текущей директории.

При конфликте настроек переменные процесса имеют приоритет над значениями `.env`; файл заполняет отсутствующие значения.

## 2. Автоматические тесты

Из корня проекта с активированным virtualenv:

```bash
python -m pytest -q
```

Тесты используют fake/mock upstream и не должны выполнять платные вызовы NanoGPT.

## 3. Запуск gateway

В первом терминале, из корня проекта:

```bash
source .venv/bin/activate
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Оставь процесс работающим. Во втором терминале из корня проекта активируй тот же virtualenv:

```bash
source .venv/bin/activate
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/v1/models
```

Ожидаемые результаты: публичный health возвращает `{"status":"ok"}`; каталог содержит публичные модели, например `deepseek`. Эти две проверки не запускают генерацию. Если каталог расширяется в `app/models_catalog.py`, обнови список моделей и в `opencode.jsonc` — иначе клиент не увидит новые ID. Если запросить health с валидным gateway key, ответ дополнительно содержит `upstream_mode` (`stub` или `nanogpt`); без авторизации режим не раскрывается.

## 4. Ограниченный реальный smoke test

После успешных локальных проверок выполни во втором терминале:

```bash
python scripts/smoke_gateway.py
```

Скрипт проверяет health и публичный каталог, затем делает **не более двух платных запросов** к публичной модели `deepseek`: один короткий sync и один короткий SSE с `stream_options.include_usage=true`. Если sync-проверка не проходит, SSE-запрос не отправляется. Автоматических повторов нет.

Скрипт проверяет HTTP-ответы, SSE chunks, `[DONE]` и отсутствие внутреннего `x_nanogpt_pricing` в ответе. Наличие usage выводится как наблюдение: его отсутствие само по себе не доказывает нулевую стоимость.

Если SSE оборвался после HTTP 200 и скрипт сообщает о неизвестном исходе, не запускай повтор автоматически: сначала изучи лог gateway и request IDs, которые доступны в нём. Реальный smoke test не проверяет искусственные ошибки и disconnect: эти сценарии покрываются локальными тестами с fake upstream.

## 5. Сравнение direct NanoGPT и gateway

Чтобы сравнить задержки и SSE-метаданные на одинаковом сценарии, сначала запусти gateway локально и проверь, что `/health` доступен. Пример небольшого tool-call запроса находится в `examples/tool_call_ab_request.json`.

Из корня проекта:

```bash
python scripts/compare_direct_gateway.py examples/tool_call_ab_request.json --confirm-paid
```

**Важно:** команда намеренно отправляет до двух платных запросов: один напрямую в NanoGPT, второй через gateway. `--confirm-paid` обязателен. Скрипт не исполняет возвращённый инструмент, не повторяет запросы и не печатает prompt, reasoning, ответ или аргументы инструмента. Если прямой вызов завершается ошибкой, таймаутом или неполным SSE без `[DONE]`, запрос через gateway не запускается. Если исход неизвестен, не запускай скрипт повторно, пока не проверишь логи и request ID.

Сравнивай `response_open_ms`, `first_event_ms`, `first_reasoning_ms`, `first_tool_call_ms`, `max_event_gap_ms`, `finish_reasons`, `usage` и request IDs. Это два независимых вызова модели, а не воспроизведение одной генерации: текст и длительность reasoning могут различаться даже при одинаковом payload. Для воспроизводимости сохраняй один и тот же входной JSON; для проверки реального случая можно заменить его уменьшенной копией проблемного запроса, не включая секреты или лишний контекст.

По умолчанию прямой вызов выполняется первым. Переменная `GATEWAY_BASE_URL` позволяет задать другой адрес gateway; если она не задана, используется `http://127.0.0.1:8000/v1`.

## 6. OpenCode

В корне репозитория находится `opencode.jsonc`. Он задаёт провайдер `zer0ne`, адрес `http://127.0.0.1:8000/v1` и модели `mimo`, `deepseek`, `deepseek-last`, `glm`, `spark`, `qwen`. Держи этот список в соответствии с `app/models_catalog.py`.

Запускай OpenCode из корня репозитория, чтобы он использовал этот проектный конфиг:

```bash
opencode
```

В списке моделей выбери, например, `zer0ne/deepseek`. Конфигурация OpenCode должна использовать **ключ gateway**, совпадающий с `GATEWAY_TEST_API_KEY` в проектном `.env`. NanoGPT inference key читает только сервер gateway.

После обычного короткого запроса проверь агентный сценарий, в котором OpenCode вызывает инструмент, например чтение или изменение файла. Простая текстовая генерация не подтверждает tool calling: для этого gateway передаёт `tools`, `tool_choice` и `parallel_tool_calls` upstream и сохраняет tool-call chunks в SSE. Совместимость с конкретной моделью подтверждается только реальным запуском в OpenCode.

## 7. Диагностика

| Симптом | Что проверить |
|---|---|
| Приложение завершается при старте | Есть ли в проектном `.env` `NANOGPT_INFERENCE_BASE_URL` и `NANOGPT_API_KEY`. Значения не выводятся в ошибку намеренно. |
| `POST /v1/chat/completions` отвечает 401 | Совпадает ли ключ клиента в конфигурации OpenCode с `GATEWAY_TEST_API_KEY`. |
| Модель отвечает 404 | Используется ли публичный ID из `app/models_catalog.py` (например `deepseek`), а не upstream ID. |
| Gateway отвечает 429 | Превышен `GATEWAY_MAX_CONCURRENT_CHAT` или `GATEWAY_RATE_LIMIT_REQUESTS`; подними лимит или повтори позже. |
| Ответ похож на заглушку | Проверь, что в `.env` установлено `GATEWAY_UPSTREAM=nanogpt`, затем перезапусти сервер. |
| Gateway возвращает 502 | Проверь доступность NanoGPT, inference base URL и наличие доступа к выбранной модели. |
| Gateway возвращает 504 | Проверь длительность запроса и настройку `NANOGPT_TIMEOUT_SECONDS`. |
| В OpenCode нет модели | Запусти OpenCode из корня проекта и проверь `opencode.jsonc`, provider `zer0ne` и публичные ID моделей. |
| Текстовый запрос работает, инструменты нет | Убедись, что выбрана модель с tool calling; проверь тесты tool calling и поведение выбранной модели в OpenCode. |

## Контракт и границы

Inference base URL настраивается отдельно от будущей Usage/Billing API. Текущий адаптер использует относительный путь `/chat/completions` на `NANOGPT_INFERENCE_BASE_URL`. Endpoint'ы `/api/v1/usage` и Request Billing не вызываются. Внешний клиент может буферизовать SSE сам; gateway передаёт данные по мере поступления.
