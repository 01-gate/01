# NOTES_NANOGPT_CONTRACT.md

**Статус:** план контрактного smoke-теста, результаты пока не сняты  
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

### Записать по каждому host

| Проверка | Результат |
|---|---|
| HTTP status `/models` | `NOT RUN` |
| HTTP status `/models?detailed=true` | `NOT RUN` |
| Финальный URL после redirect | `NOT RUN` |
| Форма корневого JSON и путь к списку моделей | `NOT RUN` |
| Поля точного model ID | `NOT RUN` |
| Поле контекста и его единицы/семантика | `UNKNOWN` |
| Поля цены, валюта, единицы расчёта | `UNKNOWN` |
| Поля capabilities и их семантика | `UNKNOWN` |
| Отличия между host | `UNKNOWN` |

Не публиковать полный список автоматически. Для выбранной модели зафиксировать точный upstream ID и подтверждающие поля.

## 5. Выбор модели и контроль стоимости

Выбрать один model ID из ответа каталога. До генерации:

1. Проверить цену и условия в интерфейсе NanoGPT или другом авторитетном источнике для конкретной модели.
2. Проверить, что предполагаемая стоимость короткого запроса приемлема.
3. Установить точный ID в shell-переменную, не меняя регистр и не подставляя alias нашего gateway.

```bash
export NANOGPT_MODEL_ID='<EXACT_MODEL_ID_FROM_LIVE_CATALOG>'
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

## 9. Таблица результатов

Заполнить по фактическим результатам. До выполнения все значения `NOT RUN` / `UNKNOWN`.

| Область | Факт / наблюдение | Статус |
|---|---|---|
| Host `nano-gpt.com` | `NOT RUN` | NOT RUN |
| Host `api.nano-gpt.com` | `NOT RUN` | NOT RUN |
| Каталог и форма JSON | `NOT RUN` | NOT RUN |
| `detailed=true`: ID / context / pricing / capabilities | `NOT RUN` | NOT RUN |
| Точный upstream model ID | `NOT RUN` | NOT RUN |
| Sync: HTTP status и content type | `NOT RUN` | NOT RUN |
| Sync: JSON `id` / response headers / `X-Request-ID` | `NOT RUN` | NOT RUN |
| Sync: usage shape и значения | `NOT RUN` | NOT RUN |
| Sync: cost / currency / pricing metadata | `NOT RUN` | NOT RUN |
| Stream: HTTP status и content type | `NOT RUN` | NOT RUN |
| Stream: usage chunk с `include_usage` | `NOT RUN` | NOT RUN |
| Stream: `[DONE]` / терминальное событие | `NOT RUN` | NOT RUN |
| Stream disconnect / billing outcome | `UNKNOWN` | UNKNOWN |
| `GET /usage` с inference key | `NOT RUN` | NOT RUN |
| Request Billing с inference key | `NOT RUN` | NOT RUN |
| Значение billing `404` | `UNKNOWN` | UNKNOWN |

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

## 12. Решения, которые нельзя принимать по результатам одного smoke

- Не выводить, что цена в `usage.cost` равна итоговому расходу аккаунта, пока это не сверено.
- Не считать `x_nanogpt_pricing` стабильным публичным контрактом без документации/повторяемых проверок.
- Не считать отсутствие usage доказательством бесплатного вызова.
- Не считать timeout, disconnect или billing lookup `404` доказательством того, что upstream не выполнил запрос.
- Не делать слепой retry платной генерации.
- Не считать поддержку `stream_options.include_usage` универсальной для всех моделей.
- Не включать tools, vision, Responses или Anthropic Messages в наш публичный контракт без отдельного тестирования.
- Не делать выводов о разрешённости коммерческой перепродажи только из факта технической доступности API. Условия использования и требования к обработке данных проверяются отдельно перед production.
