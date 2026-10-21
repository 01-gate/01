# zer0ne local gateway (NanoGPT)

Локальный OpenAI-compatible gateway для OpenCode. Текущий рабочий срез проксирует Chat Completions через реальный NanoGPT: sync и SSE, с локальным ключом клиента и собственным allowlist моделей.

## Что поддерживается

- `GET /health` — проверка доступности.
- `GET /v1/models` — публичный каталог разрешённых gateway моделей, без авторизации.
- `POST /v1/chat/completions` — обязательный Bearer key gateway; `stream: false` и `stream: true`.
- Публичная модель `01` сопоставлена с upstream ID `xiaomi/mimo-v2.6-flash`. Клиент передаёт `01`, а не upstream ID.
- OpenAI-compatible tool calling: `tools`, `tool_choice`, `parallel_tool_calls`, сообщения с `tool_calls` и `role: "tool"`.
- Поток передаётся без полной буферизации. При нормальном завершении gateway выдаёт один `[DONE]`; ошибка после начала ответа не превращается в успешное завершение.
- Usage сохраняется в ответе, если upstream его возвращает. Внутреннее поле `x_nanogpt_pricing` в клиентский ответ не передаётся.
- Автоматических повторов генерации нет. Если соединение оборвалось после начала запроса, исход может остаться неизвестным; smoke test не запускает повтор автоматически.

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

Ожидаемые результаты: health возвращает `{"status":"ok"}`; каталог содержит публичную модель с ID `01`. Эти две проверки не запускают генерацию.

## 4. Ограниченный реальный smoke test

После успешных локальных проверок выполни во втором терминале:

```bash
python scripts/smoke_gateway.py
```

Скрипт проверяет health и публичный каталог, затем делает **не более двух платных запросов** к модели `01`: один короткий sync и один короткий SSE с `stream_options.include_usage=true`. Если sync-проверка не проходит, SSE-запрос не отправляется. Автоматических повторов нет.

Скрипт проверяет HTTP-ответы, SSE chunks, `[DONE]` и отсутствие внутреннего `x_nanogpt_pricing` в ответе. Наличие usage выводится как наблюдение: его отсутствие само по себе не доказывает нулевую стоимость.

Если SSE оборвался после HTTP 200 и скрипт сообщает о неизвестном исходе, не запускай повтор автоматически: сначала изучи лог gateway и request IDs, которые доступны в нём. Реальный smoke test не проверяет искусственные ошибки и disconnect: эти сценарии покрываются локальными тестами с fake upstream.

## 5. OpenCode

В корне репозитория находится `opencode.jsonc`. Он задаёт провайдер `local`, адрес `http://127.0.0.1:8000/v1`, публичную модель `01` и выбирает её как `local/01`.

Запускай OpenCode из корня репозитория, чтобы он использовал этот проектный конфиг:

```bash
opencode
```

В списке моделей выбери `local/01`. Конфигурация OpenCode должна использовать **ключ gateway**, совпадающий с `GATEWAY_TEST_API_KEY` в проектном `.env`. NanoGPT inference key читает только сервер gateway.

После обычного короткого запроса проверь агентный сценарий, в котором OpenCode вызывает инструмент, например чтение или изменение файла. Простая текстовая генерация не подтверждает tool calling: для этого gateway передаёт `tools`, `tool_choice` и `parallel_tool_calls` upstream и сохраняет tool-call chunks в SSE. Совместимость с конкретной моделью подтверждается только реальным запуском в OpenCode.

## 6. Диагностика

| Симптом | Что проверить |
|---|---|
| Приложение завершается при старте | Есть ли в проектном `.env` `NANOGPT_INFERENCE_BASE_URL` и `NANOGPT_API_KEY`. Значения не выводятся в ошибку намеренно. |
| `POST /v1/chat/completions` отвечает 401 | Совпадает ли ключ клиента в конфигурации OpenCode с `GATEWAY_TEST_API_KEY`. |
| Модель отвечает 404 | Используется ли публичный ID `01`, а не upstream ID. |
| Ответ похож на заглушку | Проверь, что в `.env` установлено `GATEWAY_UPSTREAM=nanogpt`, затем перезапусти сервер. |
| Gateway возвращает 502 | Проверь доступность NanoGPT, inference base URL и наличие доступа к выбранной модели. |
| Gateway возвращает 504 | Проверь длительность запроса и настройку `NANOGPT_TIMEOUT_SECONDS`. |
| В OpenCode нет модели | Запусти OpenCode из корня проекта и проверь `opencode.jsonc`, provider `local` и model `01`. |
| Текстовый запрос работает, инструменты нет | Убедись, что выбран `local/01`; проверь тесты tool calling и поведение выбранной модели в OpenCode. |

## Контракт и границы

Inference base URL настраивается отдельно от будущей Usage/Billing API. Текущий адаптер использует относительный путь `/chat/completions` на `NANOGPT_INFERENCE_BASE_URL`. Endpoint'ы `/api/v1/usage` и Request Billing не вызываются. Внешний клиент может буферизовать SSE сам; gateway передаёт данные по мере поступления.
