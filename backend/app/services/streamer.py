import os
import json
from typing import Callable, Optional
import httpx

# 設定與環境變數
MODEL = os.environ.get("OLLAMA_MODEL", "gpt-oss:20b")
BASE = os.environ.get("OLLAMA_URL") or "http://127.0.0.1:8008"
ENDPOINT = BASE.rstrip("/") + "/api/generate"
DEBUG = os.environ.get("OLLAMA_DEBUG") == "1"


def _debug(*args):
    """當 DEBUG 為 True 時列印診斷訊息（僅 server 端 log，不會送給前端）"""
    if DEBUG:
        print("[streamer DEBUG]", *args)


def _extract_text_from_part(part: dict) -> Optional[str]:
    """
    從模型回傳的 JSON 片段中抽出最有意義、對使用者可見的文字欄位。
    """
    for key in ("response", "response_text", "text", "output", "content"):
        v = part.get(key)
        if isinstance(v, str) and v != "":
            return v

    choices = part.get("choices")
    if isinstance(choices, list):
        for c in choices:
            if not isinstance(c, dict):
                continue
            delta = c.get("delta") or {}
            if isinstance(delta, dict):
                cont = delta.get("content") or delta.get("text")
                if isinstance(cont, str) and cont != "":
                    return cont
            ct = c.get("text")
            if isinstance(ct, str) and ct != "":
                return ct

    return None


def _build_context_from_history(history: list) -> str:
    """將對話歷史轉換為 Ollama prompt 格式"""
    context_parts = []
    for msg in history:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "user":
            context_parts.append(f"User: {content}")
        elif role == "assistant":
            context_parts.append(f"Assistant: {content}")
    return "\n".join(context_parts)


def request_stream_sync(
        user_msg: str,
        model: Optional[str],
        on_chunk: Callable[[dict], None],
        conversation_history: Optional[list] = None
) -> None:
    """
    核心同步串流函式（支援對話記憶）
    新增參數：
      - conversation_history: 對話歷史，格式為 [{"role": "user/assistant", "content": "..."}]
    """
    m = model or MODEL

    # 構建包含對話歷史的 prompt
    if conversation_history:
        context = _build_context_from_history(conversation_history)
        full_prompt = f"{context}\nUser: {user_msg}\nAssistant:"
    else:
        full_prompt = user_msg

    payload = {"model": m, "prompt": full_prompt, "stream": True}

    try:
        client = httpx.Client(timeout=None)
    except Exception as e:
        on_chunk({"type": "error", "error": f"建立 HTTP 客戶端失敗：{e}"})
        return

    try:
        with client.stream("POST", ENDPOINT, json=payload,
                           headers={"Accept": "text/event-stream, application/json"}) as resp:
            if resp.status_code != 200:
                on_chunk({"type": "error", "error": f"HTTP {resp.status_code}: {resp.text}"})
                return

            for raw in resp.iter_lines():
                if not raw:
                    continue

                line = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else raw
                line = line.strip()
                _debug("RAW LINE:", line)

                if line.startswith("data:"):
                    data = line[len("data:"):].strip()
                else:
                    data = line

                if not data:
                    continue

                if data == "[DONE]":
                    on_chunk({"type": "done"})
                    return

                try:
                    part = json.loads(data)
                except Exception:
                    text_chunk = data.strip()
                    if text_chunk:
                        on_chunk({"type": "delta", "text": text_chunk})
                    continue

                if isinstance(part, dict) and part.get("done") is True:
                    text_chunk = _extract_text_from_part(part)
                    if text_chunk:
                        on_chunk({"type": "delta", "text": text_chunk})
                    on_chunk({"type": "done"})
                    return

                text_chunk = _extract_text_from_part(part)
                if not text_chunk:
                    _debug("skipping non-visible chunk:", json.dumps(part, ensure_ascii=False)[:200])
                    continue

                on_chunk({"type": "delta", "text": text_chunk})

            on_chunk({"type": "done"})
            return

    except Exception as e:
        _debug("stream exception:", str(e))

        try:
            resp2 = client.post(ENDPOINT, json={"model": m, "prompt": full_prompt, "stream": False}, timeout=30)
        except Exception as e2:
            on_chunk({"type": "error", "error": f"Ollama 連線失敗：{e2}"})
            return

        if resp2.status_code != 200:
            on_chunk({"type": "error", "error": f"HTTP {resp2.status_code}: {resp2.text}"})
            return

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
                text = json.dumps(j, ensure_ascii=False)
        except Exception:
            text = resp2.text or ""

        if not text:
            on_chunk({"type": "error", "error": "模型回傳但無文字"})
            return

        step = 80
        for i in range(0, len(text), step):
            on_chunk({"type": "delta", "text": text[i: i + step]})
        on_chunk({"type": "done"})
        return