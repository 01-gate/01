# Локальный тестовый контур gateway (без NanoGPT)

**Статус:** черновик на утверждение (pre-implementation gate)  
**Дата:** 2026-10-21  
**Область:** только локальное окружение OpenCode ↔ FastAPI stub  
**Вне скоупа:** проксирование в NanoGPT, PostgreSQL, биллинг, резервы, reconciliation, rate limits, Management API

---

## 1. Цель

Сделать минимальный локальный OpenAI-compatible API, с которым OpenCode (`@ai-sdk/openai-compatible`) стабильно работает end-to-end:

- `GET /v1/models`
- `POST /v1/chat/completions` (sync и `stream: true` через SSE)
- проверка одного тестового клиентского API-ключа из environment

На границе «здесь будет upstream NanoGPT» — **заглушка** с фейковыми, но по форме реалистичными ответами (id, object, model, choices/delta, finish_reason, usage). Реальный HTTP-вызов наружу **запрещён**.

**Критерий успеха:** из OpenCode с `baseURL: http://127.0.0.1:8000/v1` и тестовым ключом уходят запросы, приходят читаемые ответы (включая stream + usage), невалидный ключ даёт 401 **до** любой генерации.

---

## 2. Зафиксированные решения (квиз)

| Тема | Решение |
|------|---------|
| Скоуп | Stub API + клиентский auth |
| Поведение stub | Реалистичный SSE (чанки + финальный usage) |
| Пути | Только `/v1/*` (не `/api/v1`) |
| Auth | Один тестовый ключ из env; иначе 401 |
| Сдача | Сначала SPEC → после явного «ок / implement» — код |

**Следствие для OpenCode:** в `opencode.json` выставить:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "local": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Local Gateway",
      "options": {
        "baseURL": "http://127.0.0.1:8000/v1",
        "apiKey": "{env:GATEWAY_TEST_API_KEY}"
      },
      "models": {
        "01": { "name": "01" }
      }
    }
  }
}
```

`apiKey` в конфиге должен совпадать со значением `GATEWAY_TEST_API_KEY` на сервере (или подставляться из того же env).

---

## 3. Стек и расположение

- Python 3.12+ / FastAPI / Uvicorn
- Без PostgreSQL, Redis, httpx-к upstream на этом этапе
- Точка входа: `app.main:app` (или эквивалент в репозитории проекта)
- Конфиг только через environment (см. §6)

**Допущение:** репозиторий локальной разработки — тот, откуда уже запускался inspector (`uvicorn app.main:app --host 127.0.0.1 --port 8000`). SPEC не привязан к абсолютному пути машины.

---

## 4. Публичный HTTP-контракт

### 4.1 Общее

| Метод | Путь | Auth | Назначение |
|-------|------|------|------------|
| GET | `/health` | нет | liveness |
| GET | `/v1/models` | нет — публичный (решение SPEC_004 §2.4) | каталог публичных моделей |
| POST | `/v1/chat/completions` | Bearer обязателен | completion sync/stream |

- Префикс `/api/v1` **не** обслуживается (осознанно; клиент переводится на `/v1`).
- `Content-Type: application/json` для POST.
- Лимит тела: 1 MiB; сверх — `413` с телом ошибки OpenAI-style.

### 4.2 Auth

1. Заголовок `Authorization: Bearer <token>`.
2. Сравнить `token` с `GATEWAY_TEST_API_KEY` (constant-time сравнение, напр. `hmac.compare_digest`).
3. Пустой / отсутствующий / неверный → **401**, body:

```json
{
  "error": {
    "message": "Invalid API key",
    "type": "invalid_request_error",
    "code": "invalid_api_key"
  }
}
```

4. Сырой ключ **не** логировать. Допустимо: `auth_present=true/false`, при желании только факт совпадения `auth_ok=true/false`.
5. Невалидный ключ **запрещает** вызов stub-генерации.

### 4.3 `GET /v1/models`

Ответ (форма OpenAI list):

```json
{
  "object": "list",
  "data": [
    {
      "id": "01",
      "object": "model",
      "created": 1729500000,
      "owned_by": "local"
    }
  ]
}
```

- Публичный id только из локального реестра (сейчас одна модель `"01"`).
- Upstream id **не** экспортировать.

### 4.4 `POST /v1/chat/completions` — вход

Минимальная валидация envelope:

| Поле | Правило |
|------|---------|
| `model` | обязателен, string; должен быть в локальном реестре (`01`), иначе **404** model_not_found |
| `messages` | обязателен, non-empty array; элементы — dict с `role` (string) |
| `stream` | bool, default false |
| `stream_options` | optional object; `include_usage` bool |
| прочее (`tools`, `max_completion_tokens`, `temperature`, …) | **pass-through в логи метаданных** (имена полей + факт наличия), в stub-генерацию не влияют, кроме опционального учёта в тексте |

Неизвестный `model` → 404:

```json
{
  "error": {
    "message": "The model `xyz` does not exist",
    "type": "invalid_request_error",
    "code": "model_not_found"
  }
}
```

Невалидный JSON / нет messages → 400 `invalid_request_error`.

**Запрещено:** переписывать `messages` содержимое; логировать полный prompt по умолчанию (достаточно `messages_count`, `last_role`, `model`, `stream`).

### 4.5 Sync-ответ (`stream: false` или отсутствует)

```json
{
  "id": "chatcmpl-local-<uuid4hex>",
  "object": "chat.completion",
  "created": <unix_ts>,
  "model": "01",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "<stub text>"
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": <int>,
    "completion_tokens": <int>,
    "total_tokens": <int>
  }
}
```

- `model` в ответе = **публичный** id из запроса (не upstream).
- `id` уникален на запрос.

### 4.6 Stream (`stream: true`) — SSE

`Content-Type: text/event-stream`  
`Cache-Control: no-cache`  
`Connection: keep-alive`

Последовательность событий:

1. Один или несколько chunk'ов с дельтой текста:

```text
data: {"id":"chatcmpl-local-...","object":"chat.completion.chunk","created":...,"model":"01","choices":[{"index":0,"delta":{"role":"assistant","content":"..."},"finish_reason":null}]}

```

(role в delta допустим только в первом chunk; дальше только `content`.)

2. Финальный chunk с `finish_reason: "stop"` и **при** `stream_options.include_usage == true` (или если клиент всегда шлёт include_usage — как OpenCode) — полем `usage` на этом же или отдельном chunk перед DONE:

Минимально совместимый вариант (рекомендуется):

- last content/finish chunk: `delta: {}`, `finish_reason: "stop"`
- отдельный chunk: `choices: []`, `usage: {...}`
- затем: `data: [DONE]\n\n`

Либо usage в том же chunk, что finish — оба варианта встречаются у провайдеров; executor выбирает один и **фиксирует тестом**. Предпочтение: **usage в отдельном chunk после finish**, как у OpenAI при `include_usage`.

Правила:

- Без полной буферизации всего «ответа» как одного JSON: писать chunk'и по мере «генерации» (можно `asyncio.sleep(0)` между словами).
- Текст stub режется по пробелам (слова) → по одному слову (+ пробел) на chunk.
- Клиентский disconnect: генератор останавливается; ошибки клиенту не нужны.
- Не вызывать внешний HTTP.

### 4.7 Текст stub

Фиксированный шаблон, достаточно «реалистичный»:

```text
[local-stub] model=01 ok. Last user: <truncated last user text to 200 chars>
```

- `last user text` = content последнего message с `role=="user"`; если content не string — `"(non-text)"`.
- Если user-сообщений нет — `Last user: (none)`.

### 4.8 Usage (фейковый)

Детерминированная оценка **без** tiktoken (чтобы не тащить зависимость на этом этапе):

- `prompt_tokens = max(1, sum(len(str(m.get("content",""))) for m in messages) // 4)`
- `completion_tokens = max(1, len(stub_text) // 4)`
- `total_tokens = prompt_tokens + completion_tokens`

Это **не** биллинг и не истина; только форма поля для OpenCode.

### 4.9 Tools

Если в body есть `tools` — stub **не** обязан возвращать `tool_calls`. Всегда текстовый assistant content + `finish_reason: stop`.  
(Расширение tools-aware — отдельная задача, вне SPEC_LOCAL.)

---

## 5. Граница «будущий upstream»

В коде явно выделить модуль/функцию, например:

```text
app/upstream/stub.py   →  generate_chat_completion(...)
# позже: app/upstream/nanogpt.py  с тем же интерфейсом
```

Сигнатура (логическая):

```text
async def run_chat(public_model_id, messages, stream: bool, include_usage: bool, extra: dict) -> 
    ChatCompletion | AsyncIterator[ChatCompletionChunk]
```

На этом этапе **единственная** реализация — stub.  
Импорт httpx/aiohttp к NanoGPT **не** добавлять.  
Комментарий в коде: `shortcut: no real upstream; replace with NanoGPT client after smoke contract`.

---

## 6. Environment

| Переменная | Обязательна | Default | Назначение |
|------------|-------------|---------|------------|
| `GATEWAY_TEST_API_KEY` | да (для auth) | нет | единственный валидный Bearer token |
| `HOST` | нет | `127.0.0.1` | bind |
| `PORT` | нет | `8000` | bind |
| `LOG_LEVEL` | нет | `INFO` | logging |

Без `GATEWAY_TEST_API_KEY` приложение при старте **логирует warning** и отклоняет все защищённые запросы с 401 (не падает process — удобнее для отладки; альтернатива fail-fast — optional, default = soft).

Upstream NanoGPT env (`NANOGPT_*`) **не** читать на этом этапе.

---

## 7. Логирование

Структурированные строки (как сейчас inspector), минимум:

- method, path, status
- `auth_present`, `auth_ok` (bool)
- `model`, `stream`, `messages_count`
- `request_id` (= id ответа)

Не логировать:

- полный `Authorization`
- полное тело messages / system prompts по умолчанию

---

## 8. Не-цели (explicit non-goals)

- Прокси / retries / timeout к NanoGPT
- PostgreSQL, балансы, резервы, тарифы, журнал проводок
- `GET/POST` Responses API, Anthropic Messages, embeddings, images
- Rate limiting, kill switch, метрики Prometheus
- Хеширование ключей в БД (один plaintext key из env — только для local)
- Поддержка `/api/v1` dual-prefix
- Реальный tokenizer / cost

---

## 9. Тесты и проверка вручную

### 9.1 Автотесты (минимальный набор, pytest + httpx ASGITransport)

1. `GET /health` → 200  
2. Без Authorization на `/v1/models` → **200** (публичный; изменено по SPEC_004 §4.1)  
3. Произвольный/неверный ключ на `/v1/models` → всё равно **200**  
4. На `POST /v1/chat/completions` без/с неверным ключом → 401; с верным ключом → 200  
5. `model=unknown` → 404  
6. sync completion → 200, `choices[0].message.content` содержит `[local-stub]`, usage > 0  
7. stream: parse SSE, есть content deltas, есть finish, есть usage (если include_usage), есть `[DONE]`  
8. В логах/ответах нет значения `GATEWAY_TEST_API_KEY` как подстроки (smoke на redaction)

### 9.2 Ручной smoke с OpenCode

1. Выставить `GATEWAY_TEST_API_KEY` и тот же ключ в opencode provider.  
2. `baseURL: http://127.0.0.1:8000/v1`  
3. Выбрать model `01`  
4. Отправить короткое сообщение → в UI виден stub-текст.  
5. Убедиться, что title-generator и основной agent оба завершаются (не висят на stream).

---

## 10. Критерии приёмки

- [ ] Только пути `/health`, `/v1/models`, `/v1/chat/completions`
- [ ] Auth: один env-ключ, 401 иначе, stub не вызывается
- [ ] Публичный model id `01` в list и в ответах
- [ ] Sync JSON по форме OpenAI chat.completion
- [ ] Stream SSE: deltas + finish + usage + `[DONE]`
- [ ] Нет исходящих HTTP к внешним хостам из handler'а completion
- [ ] OpenCode config path `/v1` задокументирован в SPEC (этот файл)
- [ ] pytest из §9.1 зелёные
- [ ] Ручной smoke §9.2 пройден исполнителем (зафиксировать в отчёте)

---

## 11. План реализации (после approval)

Порядок одним вертикальным срезом:

1. Env + auth dependency  
2. Реестр моделей (`01`)  
3. `GET /v1/models`  
4. Stub generator (sync + async stream)  
5. `POST /v1/chat/completions` branch stream/non-stream  
6. Убрать/не публиковать `/api/v1` handlers  
7. pytest §9.1  
8. Краткая инструкция запуска в README или комментарии к SPEC

**Не делать** в том же PR: NanoGPT client, БД, биллинг.

---

## 12. Pre-implementation approval gate

**Исполнителю запрещено писать/менять production-код до явного одобрения пользователя** формулировкой вроде: `ок`, `approved`, `implement`, `делай`.

Перед кодом исполнитель кратко подтверждает:

- скоуп = stub + env auth, paths only `/v1`
- stream = word-chunk SSE + fake usage
- OpenCode baseURL нужно сменить на `.../v1`
- NanoGPT не подключаем

После реализации отчёт:

1. Что сделано  
2. Файлы  
3. Какие тесты/команды и результат  
4. Оставшиеся риски  
5. Следующий шаг (контракт NanoGPT smoke / маппинг model / billing)

---

## 13. Открытые пункты (не блокируют SPEC_LOCAL)

| Пункт | Статус |
|-------|--------|
| Точный wire-format tool_calls от OpenCode | вне скоупа |
| Нужен ли fail-fast при отсутствии env key | soft 401 (зафиксировано) |
| Dual path `/api/v1` | отклонён |
| Реальный tokenizer | отклонён |
| Коммерческие условия NanoGPT | до публичного биллинга |

---

## 14. История решений

- 2026-10-21: инспектор подтвердил Bearer от OpenCode; path был `/api/v1/...`.  
- 2026-10-21: квиз — только `/v1`, env key, realistic stream, stub+auth, SPEC→implement после ok.

