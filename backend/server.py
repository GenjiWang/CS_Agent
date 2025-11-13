# server.py
import os
import logging
import time
import asyncio
import json
import unicodedata
from typing import List, Iterator, Any, Callable, Tuple
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket
from starlette.websockets import WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from ollama import Client
import uvicorn
from urllib.parse import urlparse

# load env
load_dotenv()

# -------------------------
# Config (可用 .env 覆寫)
# -------------------------
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:8008")
OLLAMA_DEFAULT_MODEL = os.getenv("OLLAMA_DEFAULT_MODEL", "gpt-oss:20b")
API_KEY = os.getenv("API_KEY")
REQUEST_TIMEOUT_SECONDS = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "30"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "2"))
OLLAMA_HEADERS_RAW = os.getenv("OLLAMA_HEADERS", "")

# normalize OLLAMA_HOST if it's 0.0.0.0
def _normalize_host(host: str) -> str:
    try:
        u = urlparse(host if "://" in host else f"http://{host}")
        scheme = u.scheme or "http"
        h = u.hostname or "127.0.0.1"
        p = u.port or (443 if scheme == "https" else 80)
        if h == "0.0.0.0":
            h = "127.0.0.1"
        return f"{scheme}://{h}:{p}"
    except Exception:
        return "http://127.0.0.1:8008"

OLLAMA_HOST = _normalize_host(OLLAMA_HOST)

# -------------------------
# Logging
# -------------------------
logger = logging.getLogger("server")
handler = logging.StreamHandler()
formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(logging.INFO)

logger.info("Using OLLAMA_HOST=%s", OLLAMA_HOST)

# -------------------------
# Ollama client
# -------------------------
headers = {}
if OLLAMA_HEADERS_RAW:
    for pair in OLLAMA_HEADERS_RAW.split(";"):
        if ":" in pair:
            k, v = pair.split(":", 1)
            headers[k.strip()] = v.strip()

ollama_client = Client(host=OLLAMA_HOST, headers=headers or None)

# -------------------------
# FastAPI app
# -------------------------
app = FastAPI(title="Ollama Proxy WebSocket", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 開發環境用，正式環境請限制
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------------
# Schemas
# -------------------------
class Message(BaseModel):
    role: str
    content: str

class ChatInit(BaseModel):
    model: str | None = None
    messages: List[Message]
    x_api_key: str | None = None

# -------------------------
# Helpers
# -------------------------
def _verify_api_key_from_payload(x_api_key: str | None):
    if API_KEY and (not x_api_key or x_api_key != API_KEY):
        raise ValueError("Invalid or missing API key")

def _extract_text(item: Any) -> Tuple[str, bool]:
    """
    返回 (text, is_final).
    is_final True 表示該片段包含最終的 message.content（應立刻 flush 並 send done）。
    此實作會過濾掉常見的 thinking/meta 欄位，並保留單字中文或其他 Unicode 文字。
    """
    # bytes / str
    if isinstance(item, bytes):
        return item.decode(errors="ignore"), False
    if isinstance(item, str):
        s = item.strip()
        return (s, False) if s else ("", False)

    # dict-like
    if isinstance(item, dict):
        # 明確最終 message 物件（message.content）
        msg = item.get("message")
        if isinstance(msg, dict):
            mc = msg.get("content")
            if mc and str(mc).strip():
                return str(mc), True

        # 有些 client 會直接在 content 欄位放最終內容
        if "content" in item and item["content"] and str(item["content"]).strip():
            return str(item["content"]), True

        # delta / token 片段（小碎片） --- 保留中文單字與可見 Unicode，過濾純標點或 metadata
        delta = item.get("delta") or item.get("text") or item.get("token")
        if delta and str(delta).strip():
            low = str(delta).strip()

            # 判斷是否全為標點或控制字元（若是則跳過）
            def is_all_punct_or_control(s: str) -> bool:
                for ch in s:
                    cat = unicodedata.category(ch)
                    if not (cat.startswith("P") or cat.startswith("C")):
                        return False
                return True

            if is_all_punct_or_control(low):
                return "", False

            # 若內容看起來像 metadata（包含 "thinking"、"tool"...），忽略
            meta_kw = ("thinking", "tool", "images", "eval", "done_reason", "created_at")
            if any(k in low.lower() for k in meta_kw):
                return "", False

            # 否則視為有效非-final token（保留單字中文）
            return low, False

        return "", False

    # object-like (Message dataclass)
    msg = getattr(item, "message", None)
    if msg is not None:
        mc = getattr(msg, "content", None)
        if mc and str(mc).strip():
            return str(mc), True
    content_attr = getattr(item, "content", None)
    if content_attr and str(content_attr).strip():
        return str(content_attr), True

    # fallback: delta/text attributes but filter metadata-like
    for attr in ("delta", "text"):
        val = getattr(item, attr, None)
        if val and str(val).strip():
            low = str(val).strip()
            if any(k in low.lower() for k in ("thinking", "tool", "images", "eval")):
                return "", False
            return low, False

    return "", False

async def _stream_from_sync_iter(ws: WebSocket, sync_iter: Iterator[Any]):
    """
    Non-blocking consumption via run_in_executor + robust filtering.
    Buffer 合併短片段、過濾 metadata、識別 final content 並送 done。
    """
    MIN_FLUSH_CHARS = 12     # 調小以避免誤丟中文單字，視情況可以再調整 8~24
    CHUNK_SIZE = 512
    FLUSH_SLEEP = 0.01

    loop = asyncio.get_running_loop()
    it = iter(sync_iter)
    buffer = ""
    last_sent = ""  # 用於去重：不重複送出連續相同內容

    try:
        while True:
            # 非阻塞地取得下一個 fragment
            item = await loop.run_in_executor(None, lambda: next(it, None))
            if item is None:
                break

            text, is_final = _extract_text(item)
            if not text and not is_final:
                continue

            text = text.replace("\r", "")
            if len(text.strip()) == 0:
                continue

            if is_final:
                # flush buffer first
                if buffer and buffer.strip():
                    to_send = buffer.strip()
                    if to_send and to_send != last_sent:
                        await ws.send_text(json.dumps({"type": "delta", "text": to_send}, ensure_ascii=False))
                        last_sent = to_send
                    buffer = ""
                # send final content in chunks
                for i in range(0, len(text), CHUNK_SIZE):
                    chunk = text[i:i+CHUNK_SIZE]
                    if chunk != last_sent:
                        await ws.send_text(json.dumps({"type": "delta", "text": chunk}, ensure_ascii=False))
                        last_sent = chunk
                    await asyncio.sleep(0)
                await ws.send_text(json.dumps({"type": "done"}, ensure_ascii=False))
                return

            # accumulate non-final token
            buffer += text

            # heuristics: flush when buffer large, ends with terminal punctuation, or contains newline
            if (
                len(buffer) >= MIN_FLUSH_CHARS
                or "\n" in buffer
                or any(buffer.rstrip().endswith(p) for p in (".", "。", "!", "！", "？", "?"))
            ):
                to_send = buffer.strip()
                if to_send and to_send != last_sent:
                    await ws.send_text(json.dumps({"type": "delta", "text": to_send}, ensure_ascii=False))
                    last_sent = to_send
                buffer = ""
            else:
                # wait a little to allow more tokens to be batched
                await asyncio.sleep(FLUSH_SLEEP)

        # iterator exhausted: flush remainder
        if buffer and buffer.strip() and buffer.strip() != last_sent:
            await ws.send_text(json.dumps({"type": "delta", "text": buffer.strip()}, ensure_ascii=False))
        await ws.send_text(json.dumps({"type": "done"}, ensure_ascii=False))

    except Exception as e:
        logger.exception("Error streaming iterator over websocket: %s", e)
        try:
            await ws.send_text(json.dumps({"type": "error", "error": str(e)}))
        except Exception:
            pass

async def _send_fallback_full_response(ws: WebSocket, resp: Any, chunk_size: int = 256, delay: float = 0.02):
    try:
        text = ""
        if isinstance(resp, dict):
            text = resp.get("message", {}).get("content", "") or ""
        else:
            text = str(resp)
        for i in range(0, len(text), chunk_size):
            await ws.send_text(json.dumps({"type": "delta", "text": text[i:i+chunk_size]}, ensure_ascii=False))
            if delay:
                await asyncio.sleep(delay)
        await ws.send_text(json.dumps({"type": "done"}))
    except Exception as e:
        logger.exception("Error in fallback streaming: %s", e)
        try:
            await ws.send_text(json.dumps({"type": "error", "error": str(e)}))
        except Exception:
            pass

async def _call_sync_with_timeout_and_retries(
    fn: Callable[[], Any],
    timeout: float,
    max_retries: int,
) -> Any:
    attempt = 0
    loop = asyncio.get_running_loop()
    while True:
        try:
            fut = loop.run_in_executor(None, fn)
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError as e:
            attempt += 1
            logger.warning("Ollama call timed out (attempt %d): %s", attempt, e)
            if attempt > max_retries:
                raise
            await asyncio.sleep(0.5 * attempt)
        except Exception as e:
            attempt += 1
            logger.warning("Ollama call failed (attempt %d): %s", attempt, e)
            if attempt > max_retries:
                raise
            await asyncio.sleep(0.5 * attempt)

# -------------------------
# WebSocket endpoint (stateless, supports multiple init per connection)
# -------------------------
@app.websocket("/ws/chat")
async def websocket_chat(ws: WebSocket):
    logger.info("WS connection attempt")
    await ws.accept()
    logger.info("WS accepted")
    try:
        # 長連線迴圈：每次循環處理一個 init payload
        while True:
            init_text = await ws.receive_text()
            if init_text.strip().lower() == "close":
                logger.info("Client requested close")
                await ws.close()
                return

            try:
                init = json.loads(init_text)
            except Exception as e:
                await ws.send_text(json.dumps({"type": "error", "error": f"invalid init json: {e}"}))
                continue

            try:
                model = init.get("model") or OLLAMA_DEFAULT_MODEL
                messages = init.get("messages", [])
                x_api_key = init.get("x_api_key")
                _verify_api_key_from_payload(x_api_key)
                if not isinstance(messages, list):
                    raise ValueError("messages must be a list")
                messages_list = [{"role": m["role"], "content": m["content"]} for m in messages]
            except Exception as e:
                await ws.send_text(json.dumps({"type": "error", "error": f"init error: {e}"}))
                continue

            # 處理一次對話：stream / fallback（若失敗，回傳 error 但不關閉連線）
            try:
                # try chat_stream
                if hasattr(ollama_client, "chat_stream"):
                    sync_iter = None
                    try:
                        sync_iter = ollama_client.chat_stream(model=model, messages=messages_list)
                    except Exception:
                        sync_iter = None
                    if sync_iter is not None:
                        await _stream_from_sync_iter(ws, sync_iter)
                        continue

                # try chat(stream=True)
                if hasattr(ollama_client, "chat"):
                    sync_iter = None
                    try:
                        sync_iter = ollama_client.chat(model=model, messages=messages_list, stream=True)
                    except TypeError:
                        sync_iter = None
                    except Exception:
                        sync_iter = None
                    if sync_iter is not None and hasattr(sync_iter, "__iter__"):
                        await _stream_from_sync_iter(ws, sync_iter)
                        continue

                # fallback single call
                def sync_call():
                    return ollama_client.chat(model=model, messages=messages_list)
                resp = await _call_sync_with_timeout_and_retries(sync_call, REQUEST_TIMEOUT_SECONDS, MAX_RETRIES)
                await _send_fallback_full_response(ws, resp)
            except WebSocketDisconnect:
                logger.info("WebSocket disconnected by client during processing")
                return
            except Exception as e:
                logger.exception("Error handling one chat request: %s", e)
                try:
                    await ws.send_text(json.dumps({"type": "error", "error": str(e)}))
                except Exception:
                    pass
                # continue to next init

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected by client")
    except Exception as e:
        logger.exception("Unexpected WS error: %s", e)
        try:
            await ws.close()
        except Exception:
            pass

# -------------------------
# HTTP endpoints
# -------------------------
@app.get("/")
def root():
    return {"ok": True, "version": "0.1.0"}

@app.get("/health")
def health():
    try:
        models = ollama_client.models()
        return {"ok": True, "models_count": len(models)}
    except Exception as e:
        logger.exception("Health check failed: %s", e)
        raise HTTPException(status_code=503, detail="Ollama unreachable")

# -------------------------
# Run
# -------------------------
if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    logger.info("Starting server at %s:%d, Ollama host: %s", host, port, OLLAMA_HOST)
    uvicorn.run("server:app", host=host, port=port, reload=True)
