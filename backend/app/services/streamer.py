import os
import json
from typing import Callable, Optional
import httpx

# 設定與環境變數
MODEL = os.environ.get("OLLAMA_MODEL", "gpt-oss:20b")
# OLLAMA_URL 預設為本機，若部署改成實際地址或透過 env 設定
BASE = os.environ.get("OLLAMA_URL") or "http://127.0.0.1:8008"
ENDPOINT = BASE.rstrip("/") + "/api/generate"
# DEBUG 開關，設 OLLAMA_DEBUG=1 可在 server log 看詳細原始 chunk
DEBUG = os.environ.get("OLLAMA_DEBUG") == "1"

def _debug(*args):
    """當 DEBUG 為 True 時列印診斷訊息（僅 server 端 log，不會送給前端）"""
    if DEBUG:
        print("[streamer DEBUG]", *args)

def _extract_text_from_part(part: dict) -> Optional[str]:
    """
    從模型回傳的 JSON 片段中抽出最有意義、對使用者可見的文字欄位。
    順序優先：
      - response, response_text, text, output, content
      - choices[].delta.content 或 choices[].text
    若無可顯示文字，回傳 None（呼叫方會忽略該 chunk）
    """
    # 優先檢查常見欄位
    for key in ("response", "response_text", "text", "output", "content"):
        v = part.get(key)
        if isinstance(v, str) and v != "":
            return v

    # 檢查 choices（常見於 streaming schema）
    choices = part.get("choices")
    if isinstance(choices, list):
        for c in choices:
            if not isinstance(c, dict):
                continue
            # delta.content 或 delta.text 有時放在 delta 裡
            delta = c.get("delta") or {}
            if isinstance(delta, dict):
                cont = delta.get("content") or delta.get("text")
                if isinstance(cont, str) and cont != "":
                    return cont
            # fallback 到 choices.text
            ct = c.get("text")
            if isinstance(ct, str) and ct != "":
                return ct

    # 沒有 user-visible 的文字
    return None

def request_stream_sync(user_msg: str, model: Optional[str], on_chunk: Callable[[dict], None]) -> None:
    """
    核心同步串流函式（設計放在背景 thread 中執行）
    參數：
      - user_msg: 傳給模型的 prompt / 使用者訊息
      - model: 可選的模型名稱，若 None 則使用 MODEL
      - on_chunk: 回呼函式，用於把處理後的 chunk 交回上層（WebSocket handler）
                  回傳格式僅支援三類：
                    {"type":"delta","text": "..."}
                    {"type":"done"}
                    {"type":"error","error":"..."}
    行為：
      1. 使用 httpx.Client.stream 發出 POST 並逐行讀取 resp.iter_lines()
      2. 解析每行（支援 SSE "data:" 與直接 JSON line）
      3. 只 forward 使用者可見文字，忽略 thinking/context 類內部欄位
      4. 若 stream 失敗，退回 non-stream 一次性呼叫並分段模擬 stream
    """
    m = model or MODEL
    payload = {"model": m, "prompt": user_msg, "stream": True}

    # 嘗試建立 httpx 客戶端
    try:
        client = httpx.Client(timeout=None)
    except Exception as e:
        on_chunk({"type": "error", "error": f"建立 HTTP 客戶端失敗：{e}"})
        return

    # 正常情況：嘗試以 streaming POST 讀取逐行輸出
    try:
        with client.stream("POST", ENDPOINT, json=payload, headers={"Accept": "text/event-stream, application/json"}) as resp:
            # 非 200 視為錯誤，回傳 error 給上層
            if resp.status_code != 200:
                on_chunk({"type": "error", "error": f"HTTP {resp.status_code}: {resp.text}"})
                return

            # 逐行讀取 server 回傳（支援 SSE 與 chunked JSON）
            for raw in resp.iter_lines():
                if not raw:
                    # 空行跳過（SSE 會有空行分段）
                    continue

                # httpx 不同版本回傳 bytes 或 str，統一成 str
                line = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else raw
                line = line.strip()
                _debug("RAW LINE:", line)

                # SSE 常以 "data: ..." 開頭；去掉前綴取得實際內容
                if line.startswith("data:"):
                    data = line[len("data:"):].strip()
                else:
                    data = line

                if not data:
                    # 空 payload 跳過
                    continue

                # 特殊標記 [DONE] 表示 stream 完成（或模型實作可能用 JSON done）
                if data == "[DONE]":
                    on_chunk({"type": "done"})
                    return

                # 優先解析 JSON（大部分情況 Ollama 會回 JSON 行）
                try:
                    part = json.loads(data)
                except Exception:
                    # 若非 JSON（極少見），把純文字當 delta forward
                    text_chunk = data.strip()
                    if text_chunk:
                        on_chunk({"type": "delta", "text": text_chunk})
                    continue

                # 某些 chunk 會直接包含 done=true，先處理並嘗試把可見文字 flush
                if isinstance(part, dict) and part.get("done") is True:
                    text_chunk = _extract_text_from_part(part)
                    if text_chunk:
                        on_chunk({"type": "delta", "text": text_chunk})
                    on_chunk({"type": "done"})
                    return

                # 只抽取 user-visible 的文字欄位，忽略 thinking/context
                text_chunk = _extract_text_from_part(part)
                if not text_chunk:
                    # debug 模式下把被忽略的 chunk 部分列出（方便排查）
                    _debug("skipping non-visible chunk:", json.dumps(part, ensure_ascii=False)[:200])
                    continue

                # 把乾淨的文字 delta 發回上層處理（on_chunk 會由 ws handler 把它送到前端）
                on_chunk({"type": "delta", "text": text_chunk})

            # 若 loop 正常結束（stream 自然終止），主動送 done
            on_chunk({"type": "done"})
            return

    except Exception as e:
        # 若 streaming 過程出錯，記錄 debug 並 fallback 到一次性呼叫
        _debug("stream exception:", str(e))

        # fallback: non-stream 一次取回完整回應，並把文字分段模擬 streaming 發送
        try:
            resp2 = client.post(ENDPOINT, json={"model": m, "prompt": user_msg, "stream": False}, timeout=30)
        except Exception as e2:
            on_chunk({"type": "error", "error": f"Ollama 連線失敗：{e2}"})
            return

        # 非 200 視為錯誤
        if resp2.status_code != 200:
            on_chunk({"type": "error", "error": f"HTTP {resp2.status_code}: {resp2.text}"})
            return

        # 嘗試以 JSON 解析主要文字欄位；若沒有則用整個 JSON 字串作為最後退路
        try:
            j = resp2.json()
            text = (
                j.get("response")
                or j.get("text")
                or j.get("output")
                or j.get("content")
                or None
            )
            if not text:
                # 最後退路：整個 json stringify（不常用，僅在極端情況）
                text = json.dumps(j, ensure_ascii=False)
        except Exception:
            text = resp2.text or ""

        if not text:
            on_chunk({"type": "error", "error": "模型回傳但無文字"})
            return

        # 把完整文字切成步進塊分次發送，模擬 streaming 體驗
        step = 80
        for i in range(0, len(text), step):
            on_chunk({"type": "delta", "text": text[i : i + step]})
        on_chunk({"type": "done"})
        return
