import json
def json_dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)
