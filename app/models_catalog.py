"""Единый источник правды по моделям (SPEC_008 §5 шаг 1).

Одна структура задаёт и публичный каталог `GET /v1/models`, и accept-list
`POST /v1/chat/completions`, и маппинг public id -> upstream id. Ключи dict —
это ровно те публичные ID, которые gateway принимает и публикует.
"""

# Публичный набор зафиксирован statement of intent (SPEC_008 §10):
# mimo, deepseek, deepseek-last, glm, spark, qwen. Остальные ключи
# прежнего MODEL_UPSTREAM в публичный каталог не входят.
MODEL_CATALOG: dict[str, str] = {
    "mimo": "xiaomi/mimo-v2.6-flash",
    "deepseek": "deepseek/deepseek-v4.1-flash",
    "deepseek-last": "deepseek/deepseek-v4-flash-latest",
    "glm": "z-ai/glm-5.3-flash",
    "spark": "meta/muse-spark-1.3-contributor",
    "qwen": "qwen/qwen3.8-flash",
}
