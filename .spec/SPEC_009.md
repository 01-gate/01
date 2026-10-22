# SPEC_009: Prod-срез для личного боевого запуска — логи, токены, JSONL, env

**Версия:** 1.0  
**Дата:** 2026-10-22  
**Статус:** к реализации — ждать явного approval перед правками кода  
**Приоритет:** Высокий  
**Основание:** Pre-billing (SPEC_008) закрыт на `main`; gateway готов к личному боевому использованию. Текущий INFO (`nano_log`) перегружен SSE-диагностикой; usage/pricing NanoGPT собираются частично (`usage_seen`, `billing_lookup_id`), но не нормализуются в формат, удобный для будущего × price.  
**Область:** уровни логов INFO/DEBUG; нормализация токенов и upstream cost; обогащение JSONL; производные метрики; безопасные prod-defaults и env. **Вне scope:** Docker, systemd unit, CI, reverse-proxy, TLS, multi-instance, PostgreSQL, резервы, тарифы, списания.

---

## 1. Цель

Подготовить текущий gateway к **личному продакшн-запуску на сервере** (ручной деплой, без контейнеров) так, чтобы:

1. `log.info` по каждому chat давал **две короткие полезные строки**: summary + tokens/cost.
2. Текущий «жир» SSE-диагностики не пропал, а ушёл в `log.debug`.
3. Из ответов NanoGPT (`usage`, `x_nanogpt_pricing`) собирался **нормализованный** набор полей токенов и cost — готов к будущему умножению на прайс.
4. JSONL request log хранил те же нормализованные поля.
5. Env и defaults были безопасны для боевого режима (не stub по умолчанию в prod-доке, явные лимиты, путь логов).

**Outcome:** «Поднял uvicorn + `.env` на сервере → в journal/stdout вижу понятный расход токенов и latency; в `data/request_log.jsonl` те же цифры; клиенту cost не светится.»

---

## 2. Зафиксированные решения (quiz 2026-10-22)

| Тема | Решение |
|------|---------|
| Форма этапа | Полноценный prod-срез: логи, токены, JSONL, env, безопасные defaults |
| INFO-формат | **Две строки** на завершённый chat: summary, затем tokens |
| DEBUG | SSE-диагностика: `events`, `delta_keys`, `first_*_ms`, `max_event_gap_ms`, tool fragments, `sse_event_types`, `finish_reasons`, `upstream_fields` |
| Нормализация | `input`, `output`, `cache_read`, `cache_write`, `reasoning`, `total`, `upstream_cost` (+ currency если есть) |
| Клиенту | Cost/pricing **не** отдавать; strip `x_nanogpt_pricing` как сейчас; OpenAI-style `usage` в теле ответа сохраняется, если upstream его вернул |
| JSONL | Обогатить нормализованными токенами + upstream cost |
| Производные | `output_tps`, `ttft_ms`, `messages_count` + chars/roles (без текста), `cache_hit_ratio` при известных cache_read+input |
| Нет usage | Явно `usage=missing` / `cost=unknown`; не нули; не «бесплатно» |
| DevOps | Не проектировать; деплой руками владельца |

---

## 3. Факты и наблюдения (не контракт)

### 3.1. Подтверждено кодом на `main`

- INFO сейчас — одна длинная `nano_log` строка с ~20 полями (latency SSE, delta_keys, tool fragments и т.д.).
- `UpstreamMeta` хранит `usage_seen`, ids, latency; **не** хранит разложенные token counts / cost.
- `x_nanogpt_pricing` вырезается из клиентского ответа (`_strip_pricing`); из pricing берётся только `requestId` → `billing_lookup_id`.
- JSONL (`request_log.record`) пишет id, model, status, usage_seen, timestamps — **без** token/cost чисел.
- Auth upstream: `Authorization: Bearer <NANOGPT_API_KEY>`.

### 3.2. Наблюдения по полям NanoGPT (из HANDOFF / прошлых ответов; не гарантия на все модели)

В отдельных ответах встречались:

- `usage.prompt_tokens`, `completion_tokens`, `total_tokens`, `reasoning_tokens`
- `prompt_tokens_details` / `completion_tokens_details` / `cached_tokens`
- `usage.cost`, `currency`
- `x_nanogpt_pricing`: `amount`, `currency`, `costUsd`, `inputTokens`, `outputTokens`, `cacheReadInputTokens`, `cacheCost`, `requestId`, …

**Правило реализации:** маппить известные ключи в нормализованные поля; неизвестные/отсутствующие → `null` / omit в структуре, в INFO — `usage=missing` или конкретное `cost=unknown`. Не выдумывать числа. Не складывать пересекающиеся счётчики вслепую (reasoning может быть подмножеством output).

---

## 4. Ограничения (non-goals)

- Не Docker, не k8s, не systemd unit file в репозитории (достаточно короткой секции «как запустить» в README).
- Не PostgreSQL, не резервы, не тарифные таблицы, не списания с баланса gateway.
- Не менять публичный JSON body chat completion сверх уже принятого (usage OpenAI-style ок; cost/pricing — нет).
- Не логировать промпты, содержимое messages, секреты, сырые чанки.
- Не auto-retry платной генерации.
- Не считать отсутствие usage доказательством нулевой стоимости (как в AGENTS/HANDOFF).

---

## 5. Требуемое поведение

### 5.1. Нормализация usage / pricing → `TokenUsage`

Ввести структуру (dataclass или TypedDict), заполняемую в `app/upstream/nanogpt.py` при разборе sync body и финальных stream chunks:

| Поле | Смысл | Источники (пробовать по порядку, первый найденный) |
|------|--------|-----------------------------------------------------|
| `input` | input / prompt tokens | `usage.prompt_tokens`; `x_nanogpt_pricing.inputTokens` |
| `output` | output / completion tokens | `usage.completion_tokens`; `x_nanogpt_pricing.outputTokens` |
| `cache_read` | cache read tokens | `usage.prompt_tokens_details.cached_tokens`; `usage.cached_tokens`; `x_nanogpt_pricing.cacheReadInputTokens` |
| `cache_write` | cache write tokens | если есть отдельное поле в usage/pricing; иначе `null` |
| `reasoning` | reasoning tokens | `usage.reasoning_tokens`; details.reasoning_tokens; pricing-аналог |
| `total` | total tokens | `usage.total_tokens`; иначе `input+output` только если оба int (не подставлять, если один unknown) |
| `upstream_cost` | себестоимость upstream | `usage.cost`; `x_nanogpt_pricing.amount` / `costUsd` / `usdCost` — **один** выбранный источник, зафиксировать приоритет в коде и комментарии |
| `currency` | валюта cost | `usage.currency`; `x_nanogpt_pricing.currency`; default `null` |
| `usage_seen` | bool | как сейчас |
| `cost_seen` | bool | true если `upstream_cost` извлечён |

Правила:

1. Все числовые поля — `int | None` для токенов, cost — `Decimal`-совместимая строка или `float` **только для лога**; для JSONL предпочтительна строка с фиксированной точностью **или** decimal as string, чтобы потом не потерять точность. **Не использовать float как источник финансовой истины в будущих проводках** — здесь cost только наблюдаемый upstream, не ledger.
2. Не логировать и не писать в JSONL сырой полный `x_nanogpt_pricing` на INFO; raw dict допустим только в DEBUG.
3. Клиентский ответ: по-прежнему strip pricing; `usage` OpenAI-style оставлять если был.

Приоритет cost (зафиксировать в коде):

1. `usage.cost` + `usage.currency` если есть  
2. иначе `x_nanogpt_pricing.amount` + currency  
3. иначе `x_nanogpt_pricing.costUsd` / `usdCost` (currency=`USD`)  
4. иначе cost_seen=false

### 5.2. Производные метрики (gateway)

Считать при наличии данных, иначе `null`:

| Метрика | Формула / правило |
|---------|-------------------|
| `duration_ms` | уже есть |
| `ttft_ms` | `first_text_ms` если есть, иначе `first_event_ms` |
| `output_tps` | `output / (duration_ms/1000)` если output int и duration_ms > 0; иначе null |
| `messages_count` | `len(messages)` |
| `messages_chars` | сумма `len(str(content))` только для str content; non-text → 0 на сообщение; **текст в лог не писать** |
| `roles` | краткий счётчик `user:N,assistant:M,tool:K,system:S` |
| `cache_hit_ratio` | `cache_read / input` если оба int и input > 0; иначе null |

### 5.3. INFO — две строки

После завершения chat (sync success, stream finally, или error path):

**Строка 1 — summary** (пример формата, ключи стабильные):

```text
chat gateway_id=… upstream_id=… model=deepseek upstream_model=deepseek/… stream=0 status=200 outcome=succeeded duration_ms=2104 ttft_ms=180 error=-
```

**Строка 2 — tokens** (пример):

```text
tokens gateway_id=… in=1200 out=340 cache_r=50 cache_w=- reason=0 total=1540 cost=0.0012 currency=USD cost_seen=1 usage_seen=1 tps=161.6 cache_hit=0.042 msg=3 chars=4200 roles=user:2,assistant:1
```

Правила:

- Один и тот же `gateway_id` в обеих строках (корреляция).
- При `usage_seen=0`: `usage=missing` вместо чисел или все token-поля `-` + явный флаг.
- При `cost_seen=0`: `cost=unknown` (не `0`).
- Ошибки: строка 1 с `outcome=upstream_error|unknown_outcome|…` и `error=…`; строка 2 всё равно пишется, если частичные usage известны, иначе `usage=missing`.
- Stub-режим: те же две строки, token-поля из stub usage или missing — без притворства «nano cost».

### 5.4. DEBUG — текущий жир

Всё, что сейчас раздувает `nano_log`, переносится на `logger.debug` одной (или двумя) строками, например:

- `events`, `delta_keys`, `sse_event_types`, `finish_reasons`
- `first_event_ms`, `first_text_ms`, `first_tool_call_ms`, `first_reasoning_ms`, `max_event_gap_ms`
- `tool_call_events`, `tool_call_fragments`, `reasoning_events`
- `upstream_fields`, `extra_fields`, `response_open_ms`

Включать `gateway_id` и `model` для корреляции. При `LOG_LEVEL=INFO` debug не виден — ожидаемо. При `LOG_LEVEL=DEBUG` — полный диагностический след без потери.

### 5.5. JSONL enrichment

`request_log.record(...)` расширить полями (все optional / nullable):

- `input`, `output`, `cache_read`, `cache_write`, `reasoning`, `total`
- `upstream_cost`, `currency`, `cost_seen`
- `duration_ms`, `ttft_ms`, `output_tps`
- `messages_count`, `messages_chars`, `roles` (строка или объект)
- `cache_hit_ratio`

Не писать: промпт, messages content, API keys, raw pricing blob.

Обратная совместимость: старые строки без новых ключей допустимы; новые всегда с ключами (значение `null` если unknown).

### 5.6. Env и безопасные defaults (prod-oriented doc)

В `.env.example` и README зафиксировать:

| Переменная | Prod-рекомендация | Default в коде (если уже есть — не ломать тесты) |
|------------|-------------------|--------------------------------------------------|
| `GATEWAY_UPSTREAM` | `nanogpt` | сейчас `stub` в коде для тестов — **в README явно: для боя ставить nanogpt** |
| `LOG_LEVEL` | `INFO` | `INFO` |
| `GATEWAY_MAX_CONCURRENT_CHAT` | `8` (или меньше на маленьком VPS) | `8` |
| `GATEWAY_RATE_LIMIT_REQUESTS` | по вкусу; `0` = off | `0` |
| `GATEWAY_REQUEST_LOG_PATH` | абсолютный путь на сервере | `data/request_log.jsonl` |
| `NANOGPT_TIMEOUT_SECONDS` | `90`+ | `90` |
| `GATEWAY_TEST_MODEL` | только для тестов/smoke | `deepseek` |

Короткий блок в README: «Боевой запуск (без Docker)»: venv, `.env`, `uvicorn app.main:app --host 127.0.0.1 --port 8000` (или `0.0.0.0` за своим proxy), `LOG_LEVEL=INFO`, проверка `/health` с ключом → `upstream_mode=nanogpt`. **Не** обещать systemd/nginx — владелец настроит сам.

### 5.7. Где считать

| Ответственность | Модуль |
|-----------------|--------|
| Парсинг usage/pricing → TokenUsage | `app/upstream/nanogpt.py` (sync + stream final) |
| Хранение TokenUsage в meta | `UpstreamMeta` (+ поле `token_usage`) |
| Две INFO-строки + DEBUG | `app/main.py` (единая функция `log_chat_result(...)`) |
| JSONL | `app/request_log.py` |
| Msg stats (chars/roles) | `main.py` или маленький helper — **без** хранения текста |

---

## 6. Порядок реализации

### Шаг 0 — Approval gate

Исполнитель до правок подтверждает:

1. Список файлов.
2. Точный приоритет извлечения `upstream_cost`.
3. Пример двух INFO-строк (реальный шаблон).

Ждёт явного approve.

### Шаг 1 — `TokenUsage` + заполнение в nanogpt

- Dataclass / структура.
- Заполнение из sync response и из final stream usage chunk.
- Тесты на mock bodies: полный usage+pricing; только usage; только pricing; пусто → missing; не складывать мусор.

### Шаг 2 — Производные метрики + msg stats

- Счётчики в meta или в helper при вызове log.
- Тесты на tps/ttft/cache_hit при известных входах; null при делении на ноль / missing.

### Шаг 3 — INFO 2 lines + DEBUG перенос

- Заменить текущий длинный `nano_log` info на две строки.
- Debug — прежняя детализация.
- Тесты: caplog на INFO не содержит `delta_keys`; содержит `in=` / `usage=missing`; DEBUG при включённом уровне — содержит events.

### Шаг 4 — JSONL enrichment

- Расширить `record()`; тесты на наличие ключей и отсутствие секретов/промпта.

### Шаг 5 — README / `.env.example`

- Prod-запуск одной страницей; описание логов; LOG_LEVEL.

### Шаг 6 — Полный pytest зелёный

Hard gate: `python -m pytest -q` → 0 failed.

---

## 7. Критерии приёмки

1. При успешном sync/stream с usage в INFO ровно **две** строки на запрос с `gateway_id`, model, duration, token-полями / cost.
2. При отсутствии usage: явный `usage=missing` (или эквивалент), `cost=unknown` если cost нет; outcome не подразумевает «бесплатно».
3. `LOG_LEVEL=INFO` не печатает `delta_keys` / `sse_event_types` / tool fragment counts.
4. `LOG_LEVEL=DEBUG` сохраняет прежнюю диагностическую детализацию (или эквивалент).
5. Клиентский JSON **без** `x_nanogpt_pricing`; OpenAI `usage` сохраняется если был.
6. JSONL содержит нормализованные token/cost поля (nullable).
7. `output_tps`, `ttft_ms`, `cache_hit_ratio`, msg stats считаются по правилам §5.2 или null.
8. Полный pytest зелёный; платный smoke не обязателен для merge, но формат логов должен быть проверен хотя бы mock-тестами.
9. README описывает боевой запуск без Docker и смысл INFO-строк.

---

## 8. Риски и допущения

| ID | Тип | Содержание |
|----|-----|------------|
| A1 | Допущение | Набор ключей pricing/usage из §3.2 покрывает текущие модели каталога; новые ключи → null до расширения маппинга |
| A2 | Допущение | `upstream_cost` — наблюдаемая себестоимость NanoGPT, не цена для клиента gateway |
| A3 | Допущение | Две текстовые INFO-строки достаточно; JSON-logs не требуются на этом этапе |
| R1 | Риск | Reasoning ⊆ output у части моделей → не суммировать input+output+reasoning как «все токены биллинга» без проверки |
| R2 | Риск | Stream без final usage chunk → usage=missing даже при успехе; это ожидаемо и должно быть видно в INFO/JSONL |
| R3 | Риск | Слишком длинные roles/chars на огромных тредах — ограничить вывод roles компактной формой, chars только числом |

---

## 9. Pre-implementation approval gate

Перед кодом исполнитель присылает:

1. Файлы к изменению.
2. Приоритет cost-источников (как в §5.1).
3. Литеральный шаблон двух INFO-строк.
4. Подтверждение: клиенту cost не отдаём; Docker/systemd не добавляем.

После approve — шаги 1→6, отчёт (§10).

---

## 10. Отчёт исполнителя (после реализации)

**Статус:** реализовано, проверено, применено в проект после `approve`.

Патч: `/tmp/opencode/SPEC_009.patch` (1144 строки, 52069 байт, 7 файлов).
Применён командой `patch -p1 < /tmp/opencode/SPEC_009.patch` в `/home/pin/dev/zer0ne.ru`.

### Изменённые файлы

- `app/upstream/nanogpt.py` — `TokenUsage`, `extract_token_usage`, `merge_token_usage`, `_as_int`, `_first_int`, `_as_cost`, `_as_currency`; `UpstreamMeta.token_usage`; merge usage на каждом sync body и stream chunk перед strip pricing.
- `app/main.py` — `_message_stats`, `_fmt`, `_derived_metrics`, `_log_chat_result`; переписан `_run_chat` для nanogpt и stub; два INFO-лога и один DEBUG; enriched JSONL; outcome `succeeded` / `upstream_error` / `client_disconnected` / `needs_reconciliation` / `unknown_outcome`.
- `app/request_log.py` — `record()` расширен полями input/output/cache_read/cache_write/reasoning/total/upstream_cost/currency/cost_seen/ttft_ms/output_tps/messages_count/messages_chars/roles/cache_hit_ratio; `duration_ms` всегда присутствует.
- `README.md` — JSONL bullet, описание двух INFO-строк и DEBUG, раздел `### Боевой запуск без Docker`.
- `.env.example` — prod-рекомендации, `LOG_LEVEL=INFO`, абсолютный путь request log.
- `tests/test_local_api.py` — assertion успешного чата переведён на `gateway_id=`; ранняя ошибка авторизации остаётся `gateway_request_id=`.
- `tests/test_nanogpt_upstream.py` — новый контракт логов; FakeUpstream режим `sync_nothing`; тесты extraction, derived metrics, двух INFO-строк, DEBUG, JSONL, missing usage/cost, upstream error.

`.gitignore` и удалённый `opencode.jsonc` — ранее существовавшие правки пользователя, задача их не меняла.

### Команды и результаты тестов

- Изолированная копия `/tmp/opencode/spec009`: `.venv/bin/python -m pytest -q` → `95 passed in 3.84s`.
- `patch -p1 --dry-run` на проекте → clean.
- После применения в проекте: `.venv/bin/python -m pytest -q` → `95 passed in 4.09s`.

### Примеры реальных INFO-строк (mock или live)

Stub-режим, `POST /v1/chat/completions`, model `deepseek`:

```text
chat gateway_id=fe7d6482-eebc-4654-8848-837b2004ef74 upstream_id=chatcmpl-local-c35928070b8d418cbc06d6b8cd6024a5 upstream_request_id=- billing_lookup_id=- model=deepseek upstream_model=- stream=0 status=200 outcome=succeeded duration_ms=0 ttft_ms=- upstream_status=- error=-
tokens gateway_id=fe7d6482-eebc-4654-8848-837b2004ef74 in=1 out=11 cache_r=- cache_w=- reason=- total=12 cost=unknown currency=- cost_seen=0 usage_seen=1 tps=- cache_hit=- msg=1 chars=2 roles=user:1
```

Live NanoGPT smoke не запускался.

### Отклонения от SPEC

- Нет. Формат двух INFO-строк, DEBUG-диагностика, нормализация usage/pricing, JSONL и env/README выполнены по §2–§9.

### Оставшиеся ограничения

- Новые pricing-ключи NanoGPT, не описанные в SPEC, дают null.
- JSONL без ротации и HA.
- Sync `ttft_ms` может быть null: нет SSE events.
- Платный live smoke против NanoGPT не проводился.
- Reconciliation-механика ограничена полями лога; автоматического повторного billing lookup нет.
