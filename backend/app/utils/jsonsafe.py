import json

def json_dumps(obj) -> str:
    # 確保所有回覆都是 JSON 字串，避免非 ASCII 亂碼（必要時可加 ensure_ascii=False）
    return json.dumps(obj, ensure_ascii=False)
