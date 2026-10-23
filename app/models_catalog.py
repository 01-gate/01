"""Единый источник правды по моделям (SPEC_008 §5 шаг 1).

MODEL_CATALOG — упорядоченный список записей. Порядок записей задаёт
порядок и нумерацию в генерируемом OpenCode-конфиге (01, 02, ...).
Поля:
  id          — публичный ID gateway (принимается и публикуется)
  url         — upstream-идентификатор для nano-gpt
  name        — человекочитаемое имя БЕЗ порядкового префикса
  description — короткое описание для конфига

Порядок совпадает с порядком TARGET_IDS в scripts/extract_models.py
(он же — источник порядка для build_config.py на следующем этапе).
"""

MODEL_CATALOG: list[dict[str, str]] = [
    {
        "id": "deepseek",
        "url": "deepseek/deepseek-v4.1-flash",
        "name": "DeepSeek",
        "description": "Лучший баланс цены и качества без но",
    },
    {
        "id": "spark",
        "url": "meta/muse-spark-1.3-contributor",
        "name": "Muse Spark",
        "description": "Круче просто не найти, но Meta следит",
    },
    {
        "id": "deepseek-last",
        "url": "deepseek/deepseek-v4-flash-latest",
        "name": "DeepSeek Last",
        "description": "Чуть дороже, но на уровне Топов, предсказуемый результат за разумные деньги",
    },
    {
        "id": "luna",
        "url": "openai/gpt-luna-latest",
        "name": "GPT Luna",
        "description": "Дешёвый worker для коротких, чётко очерченных, но не многоходовых задач",
    },
    {
        "id": "haiku",
        "url": "anthropic/claude-haiku-latest",
        "name": "Claude Haiku",
        "description": "Быстрый, дешёвый, надёжный субагент, но не основной",
    },
    {
        "id": "glm",
        "url": "z-ai/glm-5.3-flash",
        "name": "GLM",
        "description": "Хороша для UI и скриншотов, но нестабильна",
    },
    {
        "id": "qwen",
        "url": "qwen/qwen3.8-flash",
        "name": "Qwen",
        "description": "Скорее она того не стоит, но пусть будет",
    },
    {
        "id": "minimax",
        "url": "minimax/minimax-m3.1-flash",
        "name": "MiniMax",
        "description": "Шустрая на рутине, но не перегружай",
    },
    {
        "id": "mimo",
        "url": "xiaomi/mimo-v2.6-flash",
        "name": "Mimo",
        "description": "Дёшево, но медленно и туговато",
    },
    {
        "id": "luna-pro",
        "url": "openai/gpt-6-luna-pro",
        "name": "GPT Luna Pro",
        "description": "Точечное усиление для самых сложных шагов, но не основа",
    },
    {
        "id": "hy3",
        "url": "tencent/hy3",
        "name": "Hy3",
        "description": "Чисто потестить, но всё может быть",
    },
]

# Страховка от опечатки при ручном редактировании (не «ловля дубликатов»).
assert len({m["id"] for m in MODEL_CATALOG}) == len(MODEL_CATALOG)
assert len({m["url"] for m in MODEL_CATALOG}) == len(MODEL_CATALOG)

# Совместимые вьюхи для main.py: публичный id -> upstream url.
MODEL_UPSTREAM: dict[str, str] = {m["id"]: m["url"] for m in MODEL_CATALOG}

# Обратный маппинг: upstream url -> публичный id.
UPSTREAM_TO_PUBLIC: dict[str, str] = {m["url"]: m["id"] for m in MODEL_CATALOG}
