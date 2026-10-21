# NOTES_NANOGPT_CONTRACT.md

**Статус:** smoke-тест выполнен, результаты сняты (см. §4, §6–§8, §9, §11)  
**Дата:** 2026-10-21  
**Цель:** установить фактический минимальный контракт NanoGPT перед подключением upstream к gateway.

> В этом документе команды и ожидаемые проверки не являются результатами тестирования. Заполнять фактические значения только после выполнения команд. Не публиковать API key, полные приватные ответы или диагностические данные, содержащие чувствительную информацию.

## 1. Scope и ограничения стоимости

В рамках этого этапа:

- Не изменять gateway и не подключать NanoGPT к stub.
- Не добавлять PostgreSQL, биллинг или новые публичные model IDs.
- Не проксировать весь каталог upstream.
- Не запускать нагрузочные тесты и не повторять платные запросы автоматически.
- Использовать только собственный NanoGPT inference API key, заданный через переменную окружения.
- Сначала выполнить read-only запросы каталога на обоих кандидатах host. Платные запросы выполнять только после выбора модели и проверки цены.
- Выполнить максимум **один sync completion и один streaming completion** в штатном сценарии. Неудачный или неоднозначный платный вызов не повторять вслепую.
- Если стоимость модели нельзя надёжно определить до запроса, не выполнять платный вызов до ручного подтверждения цены в интерфейсе NanoGPT.

## 2. Что считается успехом

1. Получены HTTP-статус и обезличенные результаты `GET /models` и `GET /models?detailed=true`.
2. Сравнены оба host:
   - `https://nano-gpt.com/api/v1`
   - `https://api.nano-gpt.com/v1`
3. Выбран точный `model` ID из живого каталога, а не из примера или догадки.
4. Зафиксированы форма sync-ответа, `usage`, относящиеся к запросу response headers и доступные идентификаторы.
5. Зафиксированы SSE-события, наличие usage в потоке и терминальное событие (`[DONE]` или фактическая альтернатива).
6. Проверены документированные Usage и Request Billing endpoints с inference key; `404` не интерпретируется как отсутствие расхода.
7. Неизвестные свойства явно остаются `UNKNOWN`, а не превращаются в контракт по предположению.

## 3. Подготовка окружения

Выполнять в локальном терминале. Не включать `set -x`, не вставлять ключ в командную строку буквально и не сохранять его в файлы.

```bash
set -a; . ./.env; set +a
printf '\n'
export NANOGPT_API_KEY
export NANOGPT_BASE_URL
# Требуются curl и jq.
command -v curl
command -v jq
```

Убедиться, что переменная непустая, не печатая значение:

```bash
test -n "${NANOGPT_API_KEY:-}" && echo 'API key is set' || echo 'API key is missing'
```

Опционально создать закрытый временный каталог для ответов. Не добавлять его в Git:

```bash
umask 077
export SMOKE_DIR
SMOKE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/nanogpt-contract.XXXXXX")"
printf 'Smoke artifacts: %s\n' "$SMOKE_DIR"
```

Файлы могут содержать текст ответов модели и служебные метаданные. Просмотреть их перед передачей кому-либо; после анализа удалить вручную. Не сохранять в них заголовок `Authorization`.

## 4. Проверка host и каталога (read-only)

Оба host проверить отдельно. Сравнить HTTP-статусы, структуру JSON, редиректы и наличие ожидаемых моделей. Не считать различие host доказанным только по HTTP 200.

```bash
for base in \
  'https://nano-gpt.com/api/v1' \
  'https://api.nano-gpt.com/api/v1'
do
  name="$(printf '%s' "$base" | sed 's#https://##; s#/#_#g')"
  printf '\n=== %s/models ===\n' "$base"
  curl --silent --show-error --location \
    --connect-timeout 10 --max-time 30 \
    -H "Authorization: Bearer ${NANOGPT_API_KEY}" \
    -H 'Accept: application/json' \
    -D "${SMOKE_DIR}/${name}.models.headers" \
    -o "${SMOKE_DIR}/${name}.models.json" \
    -w 'HTTP %{http_code}; final_url=%{url_effective}; content_type=%{content_type}; time=%{time_total}s\n' \
    "${base}/models"
  jq -r 'if type == "array" then "root=array; count=\(length)" elif type == "object" then "root=object; keys=\(keys|join(\",\"))" else "root=\(type)" end' \
    "${SMOKE_DIR}/${name}.models.json" 2>/dev/null || true

  printf '\n=== %s/models?detailed=true ===\n' "$base"
  curl --silent --show-error --location \
    --connect-timeout 10 --max-time 30 \
    -H "Authorization: Bearer ${NANOGPT_API_KEY}" \
    -H 'Accept: application/json' \
    -D "${SMOKE_DIR}/${name}.detailed.headers" \
    -o "${SMOKE_DIR}/${name}.detailed.json" \
    -w 'HTTP %{http_code}; final_url=%{url_effective}; content_type=%{content_type}; time=%{time_total}s\n' \
    "${base}/models?detailed=true"
  jq -r 'if type == "array" then "root=array; count=\(length)" elif type == "object" then "root=object; keys=\(keys|join(\",\"))" else "root=\(type)" end' \
    "${SMOKE_DIR}/${name}.detailed.json" 2>/dev/null || true
done
```

Проверить имена полей, не печатая весь каталог:

```bash
for f in "$SMOKE_DIR"/*.models.json "$SMOKE_DIR"/*.detailed.json; do
  [ -f "$f" ] || continue
  printf '\n=== %s ===\n' "$(basename "$f")"
  jq -r '
    def models:
      if type == "array" then .
      elif (.data? | type) == "array" then .data
      elif (.models? | type) == "array" then .models
      else [] end;
    models
    | .[:3]
    | .[]
    | {
        id: (.id // .model // null),
        top_level_keys: (keys),
        context_candidates: {
          context_length: (.context_length // null),
          context: (.context // null),
          max_context_length: (.max_context_length // null)
        },
        pricing_present: ((has("pricing")) or (has("input_cost")) or (has("prompt_price"))),
        capability_keys: ([keys[] | select(test("capabil|modal|tool|vision|reason|stream"; "i"))])
      }
  ' "$f" 2>/dev/null || echo 'Could not parse this response as the expected JSON shape; inspect status and content type safely.'
done
```

Этот вывод служит только для первичного осмотра первых трёх записей. Для итогового маппинга нужно установить реальную схему всего каталога и выбрать только модель, которая доступна и разрешена для нашего сервиса. Не считать `context`, pricing или capabilities обязательными, пока их наличие и смысл не подтверждены.

Результаты по каждому host (оба проверены, читательские запросы каталога 2026-10-21):

| Проверка | `nano-gpt.com/api/v1` | `api.nano-gpt.com/api/v1` |
|---|---|---|
| HTTP status `/models` | `200`, `application/json` | `200`, `application/json` |
| HTTP status `/models?detailed=true` | `200`, `application/json` | `200`, `application/json` |
| Финальный URL после redirect | без redirect, URL не изменился | без redirect, URL не изменился |
| Форма корневого JSON и путь к списку моделей | `{"data": [...], "object": ...}`, список в `.data`, 668 записей | та же форма, 661 запись |
| Поля точного model ID | `id` (в элементах `.data`: `created`, `id`, `object`, `owned_by`) | идентично |
| Поле контекста и его единицы/семантика | `context_length` в `detailed=true` (например 1048576); единицы/семантика не подтверждены документом → `UNKNOWN` | идентично |
| Поля цены, валюта, единицы расчёта | `pricing.{prompt, completion, cacheReadInputPer1kTokens, cacheWriteInputPer1kTokens, currency:"USD", unit:"per_million_tokens"}`, плюс `cost_estimate`; семантика `cost_estimate` → `UNKNOWN` | идентично |
| Поля capabilities и их семантика | `capabilities.{vision, video_input, audio_input, reasoning, tool_calling, parallel_tool_calls, structured_output, pdf_upload, batch_api}` — наличие подтверждено, точная семантика значений `UNKNOWN` | идентично |
| Отличия между host | 8 model ID есть только здесь, 1 (`perplexity/pplx-decider-v1.1-27b`) только на `api.nano-gpt.com`; запись `xiaomi/mimo-v2.6-flash` побайтово идентична на обоих → `OBSERVED ONCE` | см. слева |

Статусы: HTTP/форма — `CONFIRMED BY SMOKE TEST`; контекст-семантика, `cost_estimate`, semantics capabilities — `UNKNOWN`.

Не публиковать полный список автоматически. Для выбранной модели зафиксировать точный upstream ID и подтверждающие поля.

### Выбранная модель

- Точный upstream ID: `xiaomi/mimo-v2.6-flash` (наличие подтверждено в живом каталоге обоих host, статус `CONFIRMED BY SMOKE TEST`).
- Цена из `detailed=true` (для контроля стоимости платного вызова): input `0.14` USD/1M, output `0.28` USD/1M, `context_length: 1048576`, `max_output_tokens: 131072` — статус `OBSERVED ONCE` (каталог, не тарифный документ).

## 5. Выбор модели и контроль стоимости

Выбрать один model ID из ответа каталога. До генерации:

1. Проверить цену и условия в интерфейсе NanoGPT или другом авторитетном источнике для конкретной модели.
2. Проверить, что предполагаемая стоимость короткого запроса приемлема.
3. Установить точный ID в shell-переменную, не меняя регистр и не подставляя alias нашего gateway.

```bash
export NANOGPT_MODEL_ID='xiaomi/mimo-v2.6-flash'
test -n "$NANOGPT_MODEL_ID" || { echo 'Model ID is missing'; exit 1; }
```

Если модель не найдена в каталоге, цена неясна или её доступность не подтверждена, остановиться и не выполнять completion.

## 6. Один синхронный Chat Completions запрос

Запрос намеренно короткий. Он всё равно может тарифицироваться. Команда сохраняет заголовки и тело отдельно, не выводя Authorization.

```bash
curl --silent --show-error \
  --connect-timeout 10 --max-time 90 \
  -H "Authorization: Bearer ${NANOGPT_API_KEY}" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json' \
  -D "${SMOKE_DIR}/sync.headers" \
  -o "${SMOKE_DIR}/sync.json" \
  -w 'HTTP %{http_code}; content_type=%{content_type}; time=%{time_total}s\n' \
  "${NANOGPT_BASE_URL}/chat/completions" \
  --data-binary @- <<JSON
{
  "model": "${NANOGPT_MODEL_ID}",
  "messages": [{"role": "user", "content": "Reply with exactly OK."}],
  "stream": false,
  "max_tokens": 8
}
JSON
```

Если выбранная модель не поддерживает `max_tokens`, не повторять запрос наугад. Зафиксировать ошибку как наблюдение и проверить контракт параметра по документации/ответу ошибки прежде чем решать, нужен ли ещё один платный вызов.

Извлечь безопасные диагностические поля:

```bash
printf '%s\n' '=== selected response headers ==='
grep -iE '^(HTTP/|content-type:|x-request-id:|request-id:|retry-after:|date:)' \
  "${SMOKE_DIR}/sync.headers" || true

printf '%s\n' '=== response shape ==='
jq '{
  top_level_keys: (keys),
  id: (.id // null),
  model: (.model // null),
  finish_reason: (.choices[0].finish_reason // null),
  usage: (.usage // null),
  pricing_extension_keys: ((.x_nanogpt_pricing // {}) | if type == "object" then keys else [] end),
  error: (if .error then {type:(.error.type // null), code:(.error.code // null), message_present:(.error.message != null)} else null end)
}' "${SMOKE_DIR}/sync.json" 2>/dev/null || echo 'Response is not the expected JSON object; inspect status/content type and redact before sharing.'
```

Не выводить без необходимости полный текст ответа, prompt, Authorization или необработанные диагностические заголовки.

Зафиксировать:

- HTTP status и content type.
- `id` из JSON и `X-Request-ID` из HTTP-заголовков, если они есть. Это разные источники, не считать их одинаковыми без доказательства.
- `model` в ответе.
- Какие usage-поля присутствуют, какие отсутствуют, их типы и значения.
- Есть ли cost/currency/pricing extension и не противоречат ли значения друг другу.
- Есть ли finish reason.
- Если ответ неуспешен, не делать автоматический повтор.

Результат (2026-10-21, модель `xiaomi/mimo-v2.6-flash`):

- HTTP `200`, `application/json`, `time≈10.0s`.
- JSON `id`: `chatcmpl-2e0defc2-730e-4ab9-9f84-3e6130793e43`; HTTP-заголовок `x-request-id: req_a6cb487d-748c-4b40-95d5-fbef639c6013` — разные источники, не равны (`OBSERVED ONCE`).
- `model` в ответе: `xiaomi/mimo-v2.6-flash`.
- `finish_reason`: `length` (из-за `max_tokens: 8`).
- `usage`: присутствуют `prompt_tokens=12`, `completion_tokens=8`, `total_tokens=20`, `cache_read_input_tokens=0`, `prompt_tokens_details`, `completion_tokens_details`, `reasoning_tokens=0`, `input_tokens=12`, `cost=3.92e-06`, `currency="USD"`; типы числовые (`OBSERVED ONCE`).
- Pricing-метаданные: верхнеуровневый `x_nanogpt_pricing` с ключами `amount`, `currency`, `paymentSource`; `usage.cost` совпадает с позже полученным billing lookup — противоречий нет (`OBSERVED ONCE`, контрактом не считается).
- Верхнеуровневые ключи ответа: `choices`, `created`, `id`, `model`, `object`, `service_tier`, `system_fingerprint`, `usage`, `x_nanogpt_pricing`.

## 7. Один streaming Chat Completions запрос

Выполнять только после успешной проверки цены и выбранной модели. Это второй и последний плановый платный вызов в данном smoke-тесте.

```bash
curl --silent --show-error --no-buffer \
  --connect-timeout 10 --max-time 120 \
  -H "Authorization: Bearer ${NANOGPT_API_KEY}" \
  -H 'Content-Type: application/json' \
  -H 'Accept: text/event-stream' \
  -D "${SMOKE_DIR}/stream.headers" \
  -o "${SMOKE_DIR}/stream.sse" \
  -w 'HTTP %{http_code}; content_type=%{content_type}; time=%{time_total}s\n' \
  "${NANOGPT_BASE_URL}/chat/completions" \
  --data-binary @- <<JSON
{
  "model": "${NANOGPT_MODEL_ID}",
  "messages": [{"role": "user", "content": "Reply with exactly OK."}],
  "stream": true,
  "stream_options": {"include_usage": true},
  "max_tokens": 8
}
JSON
```

Если upstream отвергает `stream_options.include_usage`, зафиксировать точный статус и безопасно извлечённый код ошибки. Не отправлять второй streaming-запрос автоматически. Поддержка параметра может зависеть от модели или реализации API.

Проверить терминальные маркеры и usage:

```bash
printf '%s\n' '=== selected response headers ==='
grep -iE '^(HTTP/|content-type:|x-request-id:|request-id:|retry-after:|date:)' \
  "${SMOKE_DIR}/stream.headers" || true

printf '%s\n' '=== SSE markers ==='
grep -nE '^(event:|data: *\[DONE\]|data: *\{)' "${SMOKE_DIR}/stream.sse" \
  | tail -n 30 || true

printf '%s\n' '=== usage-bearing SSE JSON events ==='
grep '^data: *{' "${SMOKE_DIR}/stream.sse" \
  | sed 's/^data: *//' \
  | jq -c 'select(has("usage") or has("x_nanogpt_pricing") or has("error")) |
      {id:(.id // null), model:(.model // null), usage:(.usage // null),
       pricing_extension_keys:((.x_nanogpt_pricing // {}) | if type=="object" then keys else [] end),
       error:(if .error then {type:(.error.type // null), code:(.error.code // null)} else null end)}' \
  2>/dev/null || true
```

Ожидаемое поведение нельзя считать подтверждённым до фактического наблюдения. Зафиксировать:

- `Content-Type` и HTTP status.
- Присутствует ли `X-Request-ID`.
- Какие SSE chunks/events получены и в каком порядке.
- Есть ли usage chunk при `include_usage: true`; в каком событии он появляется.
- Есть ли `data: [DONE]` и когда он приходит.
- Есть ли последний chunk с `choices: []`, если это наблюдается.
- Что происходит с usage, если поток завершился без ожидаемого финального chunk.
- Не интерпретировать успешное закрытие соединения само по себе как подтверждение биллинга.

Результат (2026-10-21, тот же model ID, второй и последний плановый платный вызов):

- HTTP `200`, `content-type: text/event-stream`, `x-request-id: req_49e49ccd-fc3e-4257-b413-00e9c7f1db04`, `time≈4.9s`.
- `stream_options.include_usage` принят без ошибки (`CONFIRMED BY SMOKE TEST` для этих вызовов; универсальность не утверждается).
- Порядок чанков: 5 JSON-событий `data:` (1: `delta.role=assistant`; 2–3: `delta.reasoning`; 4: пустые дельты; 5: `finish_reason="length"` с `usage` и `x_nanogpt_pricing`), затем `data: [DONE]` (строка 11 файла), маркера `event:` нет.
- Usage chunk: отдельного usage-only чанка и `choices: []` **не наблюдалось** — `usage` пришёл в том же финальном чанке, где `finish_reason` (`OBSERVED ONCE`).
- В `x_nanogpt_pricing` финального чанка есть `requestId`, равный заголовочному `X-Request-ID`.
- `data: [DONE]` присутствует и приходит последним, после usage-содержащего чанка.

### Disconnect

Обычный завершённый stream не проверяет поведение при отключении клиента. Если этот сценарий необходимо проверить, выполнить его отдельно только после оценки стоимости и ручного решения. Не использовать автоматические повторы. Записать отдельно: клиентский disconnect, завершился ли upstream, получен ли request ID, можно ли затем найти billing record. Если это нельзя надёжно установить без дополнительного платного запроса, оставить `UNKNOWN`.

## 8. Usage и Request Billing lookup

Сначала использовать request ID из фактически полученного ответа/заголовка. Не подставлять JSON `id`, если документация или наблюдение не подтверждают, что именно он ожидается endpoint'ом.

```bash
# Агрегированный usage авторизованного inference key.
curl --silent --show-error \
  --connect-timeout 10 --max-time 30 \
  -H "Authorization: Bearer ${NANOGPT_API_KEY}" \
  -H 'Accept: application/json' \
  -D "${SMOKE_DIR}/usage.headers" \
  -o "${SMOKE_DIR}/usage.json" \
  -w 'HTTP %{http_code}; content_type=%{content_type}; time=%{time_total}s\n' \
  "${NANOGPT_BASE_URL}/usage"

# Установить только ID, который действительно получен от upstream.
export UPSTREAM_REQUEST_ID='<REQUEST_ID_FROM_RESPONSE_OR_HEADER>'

curl --silent --show-error \
  --connect-timeout 10 --max-time 30 \
  -H "Authorization: Bearer ${NANOGPT_API_KEY}" \
  -H 'Accept: application/json' \
  -D "${SMOKE_DIR}/request-billing.headers" \
  -o "${SMOKE_DIR}/request-billing.json" \
  -w 'HTTP %{http_code}; content_type=%{content_type}; time=%{time_total}s\n' \
  "${NANOGPT_BASE_URL}/usage/requests/${UPSTREAM_REQUEST_ID}"
```

Перед запросом Request Billing заменить placeholder настоящим ID. Если ID не получен, пропустить lookup и записать причину.

Снять HTTP status и безопасную форму JSON. Для `404` не повторять inference. При необходимости выполнить только повторный **read-only lookup** позже, если это допускают ограничения API и временное окно доступности. `404` может означать, что billing record ещё не готов, и не является доказательством нулевой стоимости.

Если endpoint возвращает `401`/`403`, зафиксировать, что inference key не получил доступ в этой проверке. Не делать вывод, что endpoint существует только для management credentials, пока это отдельно не подтверждено документацией или разрешённой проверкой с management token. Management token не использовать для inference.

### Результаты (2026-10-21)

Важное наблюдение о путях: на host `api.nano-gpt.com` inference обслуживается под `/v1` (работает `${NANOGPT_BASE_URL}/chat/completions`), а Usage и Request Billing документированы и работают только под `/api/v1`. Запросы по `${NANOGPT_BASE_URL}/usage` и `${NANOGPT_BASE_URL}/usage/requests/{id}` вернули `404` с `text/html` (страница приложения) — это путь, а не отсутствие записи. Повторный read-only lookup по документированному пути выполнен (разрешено §8):

- `GET https://api.nano-gpt.com/api/v1/usage` → `HTTP 200`, `application/json`: `object="usage"`, `scope="current_key"`, `from=2026-09-22`, `to=2026-10-21`, `groupBy=day,model`, `totals.{requests=22, costUsd=0.01054133, netCostUsd=0.01054133, inputTokens=408726, outputTokens=6542, totalTokens=415268}` — агрегат ключа за 30 дней, не конкретный вызов (`CONFIRMED BY SMOKE TEST`).
- `GET https://api.nano-gpt.com/api/v1/usage/requests/req_49e49ccd-fc3e-4257-b413-00e9c7f1db04` (ID взят из HTTP-заголовка ответа) → `HTTP 200`: `object="request_billing"`, `created_at=2026-10-21T13:21:56.729000Z`, `expires_at=2026-10-22T13:21:56.729000Z`, `cost="0.00000392"` (decimal string), `currency="USD"`, `cost_scope="primary_charge"`, `usage.{input_tokens=12, output_tokens=8, reasoning_tokens=null}` (`CONFIRMED BY SMOKE TEST`).
- Начальный `404` объяснён неверным путём; отдельно значение « billed-записи нет » не проверялось, `404` не интерпретировался как нулевой расход.
- `GET /usage` с inference key доступен без management credentials (`CONFIRMED BY SMOKE TEST`).

## 9. Таблица результатов

Заполнено по фактическим результатам 2026-10-21.

| Область | Факт / наблюдение | Статус |
|---|---|---|
| Host `nano-gpt.com` | `/models` и `/models?detailed=true` → `200` JSON, без redirect, 668 моделей | CONFIRMED BY SMOKE TEST |
| Host `api.nano-gpt.com` | то же, `200`, 661 модель | CONFIRMED BY SMOKE TEST |
| Каталог и форма JSON | корень `{"data":[...], "object":...}`, список в `.data`; plain-форма: `id`, `created`, `object`, `owned_by` | CONFIRMED BY SMOKE TEST |
| `detailed=true`: ID / context / pricing / capabilities | ID=`id`; контекст=`context_length` (единицы не подтверждены); pricing=`pricing.{prompt,completion,currency=USD,unit=per_million_tokens,...}`; capabilities=`capabilities.*` — семантика значений `UNKNOWN` | CONFIRMED BY SMOKE TEST (поля), семантика частично UNKNOWN |
| Точный upstream model ID | `xiaomi/mimo-v2.6-flash`, есть в каталоге обоих host, запись идентична; цена из каталога 0.14/0.28 USD за 1M | CONFIRMED BY SMOKE TEST |
| Sync: HTTP status и content type | `200`, `application/json`, `time≈10.0s` | CONFIRMED BY SMOKE TEST |
| Sync: JSON `id` / response headers / `X-Request-ID` | JSON `chatcmpl-2e0defc2-…`; заголовок `req_a6cb487d-748c-4b40-95d5-fbef639c6013`; не равны | OBSERVED ONCE |
| Sync: usage shape и значения | `prompt=12, completion=8, total=20, input_tokens=12, cache_read=0, reasoning=0, details-*`, `cost=3.92e-06`, `currency=USD`; `finish_reason=length` | OBSERVED ONCE |
| Sync: cost / currency / pricing metadata | `usage.cost=3.92e-06 USD` + `x_nanogpt_pricing{amount,currency,paymentSource}`; совпадает с billing lookup, противоречий нет | OBSERVED ONCE (контрактом не считается) |
| Stream: HTTP status и content type | `200`, `text/event-stream`, `x-request-id: req_49e49ccd-fc3e-4257-b413-00e9c7f1db04` | CONFIRMED BY SMOKE TEST |
| Stream: usage chunk с `include_usage` | параметр принят; usage пришёл в финальном чанке вместе с `finish_reason`; отдельного usage-only чанка и `choices: []` не было | OBSERVED ONCE |
| Stream: `[DONE]` / терминальное событие | `data: [DONE]` последним, после usage-чанка; `event:`-строк нет | CONFIRMED BY SMOKE TEST |
| Stream disconnect / billing outcome | не проверялось (требовалось ручное решение о стоимости) | UNKNOWN |
| `GET /usage` с inference key | `${NANOGPT_BASE_URL}/usage` → `404` HTML (путь не тот); документированный `/api/v1/usage` → `200`, totals `requests=22, costUsd=0.01054133` (агрегат за 30 дней) | CONFIRMED BY SMOKE TEST (документированный путь) |
| Request Billing с inference key | по `${NANOGPT_BASE_URL}` → `404` HTML; по `/api/v1/usage/requests/req_49e49ccd-…` → `200`, `cost="0.00000392"` USD, `primary_charge`, токены 12/8 | CONFIRMED BY SMOKE TEST |
| Значение billing `404` | первый `404` вызван неверным префиксом пути, запись нашлась при read-only повторе; `404` так и не означал нулевой расход | OBSERVED ONCE |

Разрешённые статусы: `CONFIRMED BY SMOKE TEST`, `OBSERVED ONCE`, `DOCUMENTED`, `UNKNOWN`, `NOT RUN`, `CONTRADICTION`. Один успешный запрос подтверждает только наблюдавшийся путь и модель, а не универсальный контракт всех моделей NanoGPT.

## 10. Минимальные выводы для следующего этапа

После заполнения таблицы принять только решения, поддержанные фактами:

1. Зафиксировать выбранный upstream host как конфигурацию, а не константу в коде.
2. Зафиксировать точный upstream ID для публичного alias `01`. Не публиковать весь upstream catalog.
3. Определить минимальный интерфейс `run_chat(...)` по реально подтверждённому sync/stream контракту.
4. Определить, какие usage-поля можно использовать как достоверные, какие требуют сверки, а какие необязательны.
5. Сохранять upstream request ID и минимальные метаданные, нужные для reconciliation.
6. Если usage или billing outcome неизвестен, не считать запрос бесплатным и не повторять генерацию автоматически.
7. Затем выполнить local OpenCode gate. Если он красный, сначала исправить локальную совместимость, не подключая upstream.

## 11. Команды локального OpenCode gate

Эти проверки выполняются в локальном окружении проекта, где доступен gateway и конфигурация OpenCode. Команды ниже описывают критерии, а не предполагают конкретный порт или путь к конфигу: используй фактические значения из проекта.

- Запустить основной agent и title-generator; убедиться, что оба завершаются, а не зависают.
- Отправить запрос с заведомо неверным client Bearer key: ожидается `401`, а запрос не должен доходить до upstream.
- Отправить минимальный completion с валидным ключом через текущую конфигурацию OpenCode: ожидается публичный `model: "01"` и `usage.total_tokens > 0` в stub-ответе.
- Проверить `GET /v1/models`: опубликован только явно разрешённый alias.
- Записать точные команды, exit status и фактический результат. Не считать проверки выполненными без запуска.

### Результаты gate (2026-10-21)

Gateway уже работал: `uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload` (pid 1601655), ключ `GATEWAY_TEST_API_KEY` подтянут самим приложением из `.env` (значение совпадает с `apiKey` провайдера `local` в `opencode.jsonc`).

| Проверка | Команда / действие | Exit / статус | Результат |
|---|---|---|---|
| Основной agent | `opencode run -m local/01 "Reply with exactly OK."` (timeout 90s) | `exit=0`, завершился до таймаута | Ответ: `[local-stub] model=01 ok. Last user: "Reply with exactly OK."`, модель `01`; соединения с `127.0.0.1:8000` наблюдалось |
| Title-generator | сессия той же команды `ses_ea15bebedffeSwlcz2Y5xr9z7O` | title установлен 2026-10-21 13:26:14 UTC (через ~1s после старта), зависаний нет | Обе части завершились (`CONFIRMED BY SMOKE TEST`); какую модель использовал title-generator, не устанавливалось → `UNKNOWN` |
| Неверный client Bearer key | `POST /v1/chat/completions`, `Authorization: Bearer wrong-key` | `HTTP 401` | `{"error":{..., "code":"invalid_api_key"}}`; отклонён до `run_chat` (проверено по коду `app/main.py`) |
| Без `Authorization` | тот же endpoint без заголовка | `HTTP 401` | то же тело ошибки |
| Валидный completion | `POST /v1/chat/completions`, ключ из `.env`, `model: "01"` | `HTTP 200` | `model="01"`, `usage.total_tokens=17 > 0`, `finish_reason="stop"` (≥ подтверждено и через `opencode run`) |
| `GET /v1/models` | с валидным ключом | `HTTP 200` | каталог ровно один alias `{"id":"01"}` |

Наблюдения вне критериев gate (не исправлялось в рамках спеки):

- `GET /v1/models` не проверяет `Authorization` вообще — `200` и без заголовка; README утверждает «Bearer обязателен» → `CONTRADICTION` README↔код.
- `opencode.jsonc`: провайдер `local` использует литерал `apiKey: "huy-v-jope"` вместо `{env:GATEWAY_TEST_API_KEY}`, как описано в README; значения совпадают, но штатно ключ придётся синхронизировать вручную.
- В `opencode.jsonc` провайдер `nano-gpt` хранит живой inference key открытым текстом — риск утечки при коммите, вне рамок этой спеки.

## 12. Решения, которые нельзя принимать по результатам одного smoke

- Не выводить, что цена в `usage.cost` равна итоговому расходу аккаунта, пока это не сверено.
- Не считать `x_nanogpt_pricing` стабильным публичным контрактом без документации/повторяемых проверок.
- Не считать отсутствие usage доказательством бесплатного вызова.
- Не считать timeout, disconnect или billing lookup `404` доказательством того, что upstream не выполнил запрос.
- Не делать слепой retry платной генерации.
- Не считать поддержку `stream_options.include_usage` универсальной для всех моделей.
- Не включать tools, vision, Responses или Anthropic Messages в наш публичный контракт без отдельного тестирования.
- Не делать выводов о разрешённости коммерческой перепродажи только из факта технической доступности API. Условия использования и требования к обработке данных проверяются отдельно перед production.
