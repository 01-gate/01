import json
import urllib.request

URL = "https://nano-gpt.com/api/v1/models?detailed=true"

TARGET_IDS = {
    "xiaomi/mimo-v2.6-flash", # Mimo | Дёшево, но медленно и туговато
    "deepseek/deepseek-v4.1-flash", # Лучший баланс цены и качества без но
    "z-ai/glm-5.3-flash", # Хороша для UI и скриншотов, но нестабильна
    "meta/muse-spark-1.3-contributor", # Круче просто не найти, но Meta следит
    "qwen/qwen3.8-flash", # Скорее она того не стоит, но пусть будет
    "minimax/minimax-m3.1-flash", # Шустрая на рутине, но не перегружай
    "tencent/hy3", # Чисто потестить, но всё может быть
    "openai/gpt-6-luna", # Дешёвый worker для коротких, чётко очерченных, но не многоходовых задач
    "anthropic/claude-haiku-5.5", # Быстрый, дешёвый, надёжный субагент, но не основной
    "openai/gpt-6-luna-pro", # Точечное усиление для самых сложных шагов, но не основа
    "deepseek/deepseek-v4.1-flash-1017" # Чуть дороже, но на уровне Топов, предсказуемый результат за разумные деньги
}

FIELDS = ["reasoning_efforts", "pricing", "effective_pricing", "cost_estimate"]


def extract_items(payload):
    """Ответ может быть списком, либо объектом с data/models/items."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "models", "items", "results"):
            if isinstance(payload.get(key), list):
                return payload[key]
        # на всякий случай: если это словарь id -> модель
        if all(isinstance(v, dict) for v in payload.values()):
            return [{"id": k, **v} for k, v in payload.items()]
    raise ValueError("Не удалось найти список моделей в ответе")


def get_model_id(item):
    # id может лежать прямо в item или внутри item["model"]
    if "id" in item:
        return item["id"]
    if isinstance(item.get("model"), dict) and "id" in item["model"]:
        return item["model"]["id"]
    if isinstance(item.get("model"), str):
        return item["model"]
    return None


def main():
    req = urllib.request.Request(
        URL,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    items = extract_items(payload)

    result = []
    for item in items:
        mid = get_model_id(item)
        if mid in TARGET_IDS:
            # значения берём из item, иначе из вложенного item["model"]
            src = item
            if all(f not in item for f in FIELDS) and isinstance(item.get("model"), dict):
                src = item["model"]
            result.append({
                "id": mid,
                **{f: src.get(f) for f in FIELDS},
            })

    # сохранить и напечатать
    with open("models.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\nНайдено моделей: {len(result)} из {len(TARGET_IDS)} запрошенных")
    missing = TARGET_IDS - {r["id"] for r in result}
    if missing:
        print("Не найдены:", sorted(missing))


if __name__ == "__main__":
    main()