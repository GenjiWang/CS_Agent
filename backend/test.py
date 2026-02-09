# test.py
import httpx, traceback

url = "http://127.0.0.1:11434/api/generate"
payload = {"model":"gpt-oss:20b","prompt":"hello","stream":True}

try:
    with httpx.Client(timeout=None) as client:
        with client.stream("POST", url, json=payload, headers={"Accept":"text/event-stream"}) as resp:
            print("status", resp.status_code)
            # httpx.iter_lines() 在某些版本回傳 bytes，直接處理兩種情況
            for raw in resp.iter_lines():
                if raw is None:
                    continue
                line = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else raw
                print("RAW:", line)
except Exception:
    traceback.print_exc()