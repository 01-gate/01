# SPEC_008: Порядок работ до биллинга (pre-billing hardening)

**Версия:** 1.0  
**Дата:** 2026-10-22  
**Статус:** реализовано в рабочем дереве (не закоммичено) — ожидает review/approval  
**Приоритет:** Высокий  
**Основание:** Аудит репозитория `01-gate/01` (2026-10-22): drift каталога моделей vs тесты/README; 37 failed / 39 passed в локальном pytest; отсутствие correlation ID, rate limits и каркаса reconciliation.  
**Область:** выравнивание каталога моделей, тестов и документации; pin зависимостей; correlation ID; базовые rate/concurrency limits; статусный каркас reconciliation **без** денег, ledger, резервов и PostgreSQL.

---

## 1. Цель

Подготовить текущий вертикальный срез к этапу C (биллинг) так, чтобы:

1. Полный unit/contract suite был **зелёным** (жёсткий gate).
2. Публичный каталог, маппинг upstream ID, README и smoke-скрипты описывали одну и ту же реальность.
3. Появились минимальные эксплуатационные опоры: correlation ID, лимиты, статусы запросов и сохранение upstream request ID — без финансовой логики.

Результат: исполнимый план и критерии приёмки. После approval исполнитель вносит изменения по шагам ниже; без approval код не менять.

**Одно предложение outcome:** привести gateway к состоянию «зелёные тесты + согласованный multi-model каталог + correlation ID + лимиты + skeleton reconciliation», чтобы можно было безопасно проектировать Stage C.

---

## 2. Зафиксированные решения (quiz 2026-10-22)

| Тема | Решение |
|------|---------|
| Форма документа | Implementation plan, готовый к исполнению после approval |
| Источник правды по моделям | Текущий multi-model map в `app/main.py` корректен; тесты и README подтягиваются к нему; публичная модель `01` **не** восстанавливается |
| Test gate | Полный suite должен быть зелёным — **жёсткий** precondition для любого дизайна/кода Stage C |
| Auth | Остаётся один shared `GATEWAY_TEST_API_KEY`; multi-key / users / revoke — **вне** scope |
| Язык SPEC | Русский, стиль SPEC_004–007 |
| В scope дополнительно | README + smoke под реальные model IDs; pin версий в `requirements.txt`; gateway correlation ID в логах/ответах; basic rate/concurrency limits; reconciliation status enum + persistence request ID **без денег** |

---

## 3. Факты (подтверждены аудитом)

1. `PUBLIC_MODELS` в `app/main.py` публикует: `mimo`, `deepseek`, `deepseek-last`.
2. `MODEL_UPSTREAM` содержит также: `glm`, `spark`, `qwen`, `minimax`, `hy3`, `luna`, `haiku`, `luna-pro` (и комментарии с ценами).
3. `POST /v1/chat/completions` принимает любой ключ из `MODEL_UPSTREAM`, даже если его нет в `PUBLIC_MODELS` — каталог и accept-list **не атомарны**.
4. Тесты (`test_local_api.py`, `test_nanogpt_upstream.py`) и README ожидают публичную модель **`01`** → массовые `404 model_not_found`.
5. Биллинга, PostgreSQL, балансов, резервов нет (явно в README).
6. Auth: один shared secret, `hmac.compare_digest`.
7. Upstream-адаптер уже собирает `UpstreamMeta` (response_id, upstream_request_id, billing_lookup_id, usage_seen и latency-метрики).

---

## 4. Ограничения (non-goals)

- Не реализовывать PostgreSQL, ledger, wallets, резервы, списания, тарифы, Decimal-деньги.
- Не вызывать Usage API / Request Billing NanoGPT в runtime (кроме уже существующих полей в meta из ответа).
- Не вводить multi-user / hashed API keys / revoke.
- Не добавлять новые публичные протоколы (Responses, Anthropic Messages, embeddings и т.д.).
- Не выполнять широкие платные нагрузки; smoke — по-прежнему ≤ 2 платных вызова и только с явным confirm.
- Не менять политику «нет auto-retry платной генерации».
- Не логировать секреты, промпты, содержимое чанков.
- Не считать Stage C открытым, пока не выполнен §11 (критерии готовности к биллингу).

---

## 5. Порядок работ (строго последовательный)

Каждый шаг завершается зелёными релевантными тестами. Следующий шаг не начинать при красном suite.

### Шаг 0 — Approval gate

Исполнитель **до любых правок** предоставляет краткий statement of intent (какие файлы тронет, какой публичный набор model ID зафиксирует, какие лимиты по умолчанию). Ждёт явного «ок / approve» от пользователя. Без этого код не менять.

### Шаг 1 — Единый источник правды по моделям

**Проблема:** `PUBLIC_MODELS` ≠ ключи, которые принимает chat; тесты на `01`.

**Требуемое поведение:**

1. Ввести **один** словарь/структуру (например `MODEL_CATALOG`), из которой:
   - строится ответ `GET /v1/models` (только `id` и поля, уже разрешённые контрактом);
   - строится map public_id → upstream_id для chat;
   - chat **отклоняет** model, отсутствующий в этом каталоге (404 `model_not_found`).
2. Зафиксировать публичный набор **как в текущем runtime-намерении кода**, не возвращаясь к `01`:
   - минимум: `mimo`, `deepseek`, `deepseek-last` с текущими upstream ID;
   - дополнительные ключи из `MODEL_UPSTREAM` (`glm`, `spark`, …) **включать в публичный каталог только если** они явно перечислены в statement of intent на шаге 0; иначе оставить вне PUBLIC (и chat их не принимать).
3. Удалить/не публиковать мёртвые TODO-комментарии с ценами из hot path; цены — не runtime-контракт этого этапа.
4. `GET /v1/models` и accept-list chat **атомарны**: одно и то же множество ID.

**Проверки:**

- `GET /v1/models` возвращает ровно зафиксированный набор.
- Chat с ID из каталога → не 404 по model.
- Chat с ID вне каталога (включая старый `01`) → 404.
- Upstream получает только mapped upstream ID, не публичный alias.

**Файлы (ожидаемо):** `app/main.py`; при необходимости маленький `app/models_catalog.py` — только если это упрощает атомарность, не ради абстракции.

### Шаг 2 — Выровнять тесты под multi-model

**Требуемое поведение:**

1. Во всех тестах заменить `model="01"` / assert `id == "01"` на актуальные публичные ID (как минимум `mimo` как default happy-path).
2. Добавить/сохранить тест: unknown model → 404, upstream **не** вызывается.
3. Сохранить покрытие: auth 401, body too large 413, sync, stream, tool calling forward, strip `x_nanogpt_pricing`, timeout→504, connect→502, no retry, no secrets in logs.
4. Mock upstream по-прежнему; платных вызовов в unit/contract нет.

**Критерий:** `python -m pytest -q` — **0 failed**.

**Файлы:** `tests/test_local_api.py`, `tests/test_nanogpt_upstream.py`, при необходимости `conftest.py`.

### Шаг 3 — README и smoke/compare скрипты

**Требуемое поведение:**

1. README: убрать утверждения про единственную модель `01`; описать фактический публичный каталог и пример curl с реальным ID (например `mimo`).
2. `scripts/smoke_gateway.py` и `scripts/compare_direct_gateway.py`: default model = публичный ID из каталога; документация скриптов не упоминает `01` как обязательный.
3. Не расширять число платных вызовов; сохранить `--confirm-paid` / лимит «не более двух».

**Критерий:** README и скрипты согласованы с шагом 1; локальные проверки health/models не требуют устаревшего ID.

### Шаг 4 — Pin зависимостей

**Требуемое поведение:**

1. Зафиксировать версии в `requirements.txt` (как минимум `fastapi`, `uvicorn`, `httpx`, `pytest`) по фактически рабочим на момент шага.
2. Не добавлять новые runtime-зависимости без необходимости (correlation/rate — stdlib + уже имеющийся httpx/FastAPI).

**Критерий:** чистый install из pin’ов поднимает приложение и зелёный pytest.

### Шаг 5 — Gateway correlation ID

**Требуемое поведение:**

1. На каждый входящий `POST /v1/chat/completions` генерировать `gateway_request_id` (UUID4 или аналог).
2. Отдавать клиенту в заголовке ответа, например `X-Request-ID` (gateway-side; не путать с upstream `X-Upstream-Request-ID`, который уже есть).
3. Включать `gateway_request_id` во все structured log-строки chat path.
4. При наличии upstream request id — логировать оба; не подменять одно другим.
5. Не логировать тело запроса/промпт.

**Проверки:**

- Ответ (sync и stream) содержит gateway `X-Request-ID`.
- Лог строки chat содержат тот же id.
- Тест: id стабилен в рамках одного запроса; разный между запросами.

### Шаг 6 — Basic rate / concurrency limits

**Требуемое поведение:**

1. Простые in-process лимиты (без Redis на этом этапе):
   - max concurrent chat-запросов на процесс (например default 8, env-переопределяемый);
   - optional simple rate: max N запросов / окно на один API key (один shared key → фактически на процесс).
2. При превышении: `429` с OpenAI-compatible error shape (`code`: `rate_limit_exceeded` или аналог уже принятого стиля ошибок).
3. Лимит проверяется **до** вызова upstream.
4. Значения по умолчанию документируются в README / `.env.example`; без магии.

**Проверки:**

- При искусственно низком concurrency limit параллельные запросы сверх лимита получают 429, upstream на отклонённых не вызывается (mock).
- Лимит не ломает sequential happy-path.

**Ограничение:** не строить distributed rate limiter; один процесс — достаточно для pre-billing.

### Шаг 7 — Reconciliation skeleton (без денег)

**Требуемое поведение:**

1. Ввести явный enum/набор статусов запроса gateway, например:  
   `accepted` | `upstream_inflight` | `succeeded` | `upstream_error` | `client_disconnected` | `unknown_outcome` | `needs_reconciliation`.
2. Для каждого chat-запроса (хотя бы в памяти процесса + structured log; **персистентность на диск/БД — минимальная**, см. ниже) фиксировать:
   - `gateway_request_id`;
   - public model id;
   - upstream model id;
   - upstream request id / billing_lookup_id, если известны;
   - финальный статус из enum;
   - usage_seen (bool);
   - timestamps start/end если доступны.
3. Минимальная персистентность **без PostgreSQL**: append-only JSONL-файл (путь из env, default под `./data/request_log.jsonl` или аналог), одна строка = один завершённый/оборванный запрос. Ротация/ретеншн — не обязательны на этом шаге; документировать ограничение.
4. Правило **unknown_outcome / needs_reconciliation**:
   - stream оборвался после HTTP 200 без штатного конца и без однозначной upstream error;
   - disconnect клиента после начала upstream;
   - usage отсутствует при ожидавшемся include_usage — **не** считать бесплатным; статус не `succeeded` с «нулевой стоимостью», а наблюдаемый `succeeded`/`unknown_outcome` с `usage_seen=false`.
5. Не вызывать billing API NanoGPT автоматически. Не списывать, не резервировать.
6. Не менять публичный JSON body chat completion сверх уже принятого контракта; статусы — для логов/файла, не обязательно новое API.

**Проверки:**

- Happy-path sync/stream → статус `succeeded`, id’ы в JSONL.
- Искусственный обрыв после старта stream → не `succeeded`; есть `unknown_outcome` или `needs_reconciliation`; upstream id сохранён если был.
- Нет секретов/промптов в JSONL.

**Файлы (ожидаемо):** небольшой модуль `app/request_log.py` (или рядом), вызовы из `app/main.py`; тесты на статусные переходы с mock upstream.

---

## 6. Порядок зависимости шагов

```
0 Approval
  → 1 Каталог моделей (атомарный)
    → 2 Тесты зелёные (hard gate)
      → 3 README + smoke
      → 4 Pin deps
      → 5 Correlation ID
      → 6 Rate / concurrency
      → 7 Reconciliation skeleton
        → §11 Gate в Stage C
```

Шаги 3–4 можно частично параллелить после зелёного suite, но 5–7 — строго после 2.

---

## 7. Критерии приёмки (Definition of Done SPEC_008)

1. `python -m pytest -q` — 0 failed (hard gate).
2. `GET /v1/models` и accept-list chat используют одно множество публичных ID; `01` отсутствует, если не был явно возвращён на шаге 0 (по quiz — не возвращается).
3. README и smoke/compare не ссылаются на устаревший единственный `01` как обязательный.
4. `requirements.txt` с pinned версиями; install + pytest воспроизводимы.
5. Каждый chat-ответ несёт gateway `X-Request-ID`; тот же id в логах.
6. Превышение concurrency/rate → 429 до upstream.
7. JSONL (или согласованный минимальный persist) содержит request id’ы и статусы; unknown outcome не маскируется под успех с нулевой стоимостью.
8. Нет PostgreSQL, нет денег, нет multi-key auth, нет auto-retry генерации.
9. Отчёт исполнителя: изменённые файлы, команды тестов, фактический вывод pytest, оставшиеся ограничения.

---

## 8. Gate в Stage C (биллинг)

Stage C (ledger, резервы, Decimal, тарифные версии, идемпотентность списаний) **запрещён**, пока не выполнены все пункты §7.

Дополнительно при входе в Stage C отдельно специфицировать (будущий SPEC):

- PostgreSQL как source of truth;
- атомарный резерв **до** upstream;
- запрет держать SQL-транзакцию открытой на время генерации;
- reconciliation workflow для `needs_reconciliation` / `unknown_outcome` с использованием сохранённых upstream request ID.

SPEC_008 только готовит **идентификаторы и статусы**, не проводки.

---

## 9. Риски и допущения

| ID | Тип | Содержание | Следствие |
|----|-----|------------|-----------|
| A1 | Допущение | Публичный набор по умолчанию = `mimo`, `deepseek`, `deepseek-last`; расширение только через statement of intent | Исполнитель не тащит все 11 ключей в PUBLIC без явного списка |
| A2 | Допущение | In-process rate/concurrency достаточно до появления нескольких инстансов | При горизонтальном scale лимиты пересмотреть |
| A3 | Допущение | JSONL на локальном диске достаточен как skeleton; не HA | Потеря файла = потеря истории статусов; для Stage C заменится PG |
| R1 | Риск | Расширение PUBLIC без обновления OpenCode config | Клиент не увидит новые id — документировать в README |
| R2 | Риск | Слишком жёсткий concurrency default ломает UX OpenCode | Default консервативный, но переопределяемый env |
| R3 | Риск | Путаница gateway `X-Request-ID` vs upstream | В логах и заголовках имена развести явно |

---

## 10. Pre-implementation approval gate (обязательно)

Перед любыми правками кода исполнитель отправляет пользователю:

1. Краткий plan: список файлов к изменению.
2. Точный список публичных model ID, которые войдут в каталог после шага 1.
3. Предлагаемые default’ы: concurrency limit, rate (если включается), путь JSONL.
4. Подтверждение: multi-key auth и деньги **не** трогаются.

Ждёт явного approve. После approve выполняет шаги 1→7, затем сдаёт отчёт по §7 п.9.

---

## 11. Критерии готовности к биллингу (checklist)

- [x] Полный pytest зелёный — `python -m pytest -q` → `84 passed`
- [x] Каталог моделей атомарный (list ≡ accept) — `app/models_catalog.py`; `GET /v1/models` и accept-list chat из одного `MODEL_CATALOG`
- [x] README/smoke согласованы — каталог `mimo/deepseek/deepseek-last/glm/spark/qwen`; smoke/compare и тесты берут модель из `GATEWAY_TEST_MODEL` (`.env`, дефолт `deepseek` через `config.test_model()`)
- [x] Dependencies pinned — `requirements.txt`: fastapi==0.156.5, uvicorn==0.54.0, pytest==9.1.1, httpx==0.28.1
- [x] Gateway correlation ID в ответах и логах — `X-Request-ID` (UUID4) во всех chat-ответах и structured-логах; `X-Upstream-Request-ID` отдельно
- [x] 429 на превышение лимитов до upstream — in-process `app/limits.py`, проверка до `run_chat`; тест с mock upstream
- [x] Статусы + persistence request ID (JSONL или согласованный минимум) — `app/request_log.py` → `data/request_log.jsonl` (env `GATEWAY_REQUEST_LOG_PATH`)
- [x] Нет скрытых списаний, резервов, PG — денег/PG/Usage API в runtime нет

Только при всех отмеченных пунктах допустим следующий SPEC на Stage C.

---

## 12. Отчёт исполнителя (заполняется после реализации)

**Статус:** реализовано, закоммичено (`0f29182`) и запушено в ветку `pre-billing`; полный прогон зелёный.

Слияние `main` в `pre-billing` (merge-commit `495efde`) ошибочно вернуло в `app/main.py` hunk проверки каталога (`MODEL_UPSTREAM` вместо `MODEL_CATALOG`), что дало 44 упавших теста в CI. Исправлено коммитом `39c7ff3` (восстановлен `app/main.py` из `0f29182`); после фикса `python -m pytest -q` → `84 passed`.

### Изменённые файлы

Новые:
- `app/models_catalog.py` — единый `MODEL_CATALOG` (public ID → upstream ID): `mimo`, `deepseek`, `deepseek-last`, `glm`, `spark`, `qwen`.
- `app/limits.py` — in-process concurrency/rate limit: `try_acquire()`/`release()`/`reset()`, env `GATEWAY_MAX_CONCURRENT_CHAT`, `GATEWAY_RATE_LIMIT_REQUESTS`, `GATEWAY_RATE_LIMIT_WINDOW_SECONDS`.
- `app/request_log.py` — статусы и append-only JSONL (`GATEWAY_REQUEST_LOG_PATH`).

Изменённые:
- `app/main.py` — каталог из `MODEL_CATALOG`, atomic accept-list + 404; `gateway_request_id` (UUID4) в заголовке `X-Request-ID` и во всех логах chat; проверка лимитов до upstream (429); статусы запроса и запись JSONL.
- `app/upstream/nanogpt.py` — upstream-подпись исправлена на `Authorization: Bearer <NANOGPT_API_KEY>` (по документации NanoGPT; ранее `x-api-key` — источник двух падавших тестов). Клиентский `Authorization` upstream не передаётся.
- `tests/test_local_api.py`, `tests/test_nanogpt_upstream.py`, `tests/test_compare_direct_gateway.py` — модель `01` → `TEST_MODEL = config.test_model()` (env `GATEWAY_TEST_MODEL`, дефолт `deepseek`; `TEST_UPSTREAM_MODEL = MODEL_CATALOG[TEST_MODEL]`); `examples/tool_call_ab_request.json` → `deepseek`; добавлены тесты: атомарность каталога, отказ `01`/upstream-ID/`luna`, correlation ID в заголовке и логах, JSONL-статусы (sync/stream/обрыв), 429 concurrency до upstream, отсутствие секретов/промпта в JSONL.
- `scripts/smoke_gateway.py`, `scripts/compare_direct_gateway.py` — модель из `config.test_model()` (`GATEWAY_TEST_MODEL`, дефолт `deepseek`), upstream ID через `MODEL_CATALOG`, проверка каталога; лимит платных вызовов и `--confirm-paid` сохранены.
- `README.md` — актуальный каталог, `X-Request-ID`, лимиты, JSONL, диагностика 429, OpenCode provider `zer0ne`.
- `app/config.py` — `test_model()` (env `GATEWAY_TEST_MODEL`, дефолт `deepseek`).
- `.env` — заполнен актуальными значениями по `.env.example` (лимиты, путь JSONL, `GATEWAY_TEST_MODEL=deepseek`).
- `.env.example` — опциональные `GATEWAY_MAX_CONCURRENT_CHAT`, `GATEWAY_RATE_LIMIT_REQUESTS`, `GATEWAY_RATE_LIMIT_WINDOW_SECONDS`, `GATEWAY_REQUEST_LOG_PATH`.
- `requirements.txt` — пины версий.
- `.gitignore` — `data/`.

### Команды и результаты тестов

```bash
python -m pytest -q
```
```
84 passed in 3.37s
```
Было до правок: `37 failed, 39 passed`.

### Отклонения от SPEC

- Публичный набор расширен до 6 ID (`mimo`, `deepseek`, `deepseek-last`, `glm`, `spark`, `qwen`) по `opencode.jsonc` — это текущий runtime-интент клиента; минимум §5 (`mimo/deepseek/deepseek-last`) выполнен. Остальные ключи прежнего `MODEL_UPSTREAM` (`minimax`, `hy3`, `luna`, `haiku`, `luna-pro`) в каталог не вошли и отклоняются chat с 404.
- `GATEWAY_RATE_LIMIT_REQUESTS` по умолчанию `0` (rate-limit выключен); активен только concurrency-лимит `GATEWAY_MAX_CONCURRENT_CHAT=8`.
- Исправлен upstream-заголовок авторизации (не был отдельным шагом плана, но требовался для зелёного §5 шага 2 и соответствия документации).

### Оставшиеся ограничения

- Лимиты in-process (на процесс), не распределённые; при нескольких инстансах не разделяются — заменяются в Stage C.
- JSONL на локальном диске — skeleton, без ротации/retention и без HA; `client_disconnected` надёжно воспроизводится только на реальных сокетах (httpx ASGITransport буферизует ответ).
- Денег/PG/резервов/Usage API/Request Billing в runtime нет — Stage C не открыт.
- Реальная совместимость моделей и tool calling подтверждается только платным smoke-прогоном; автотесты используют mock upstream.
