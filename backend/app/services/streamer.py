"""
Ollama streaming service for handling chat completions.
Supports conversation history and streaming responses.
"""
import json
import logging
from typing import Callable, Optional
import httpx

from ..config import settings

# Configure logger
logger = logging.getLogger(__name__)

# Constants
ENDPOINT = settings.ollama_url.rstrip("/") + "/api/chat"
CHUNK_SIZE = 80  # Size of text chunks for non-streaming fallback


def _debug(*args) -> None:
    """Log diagnostic messages when DEBUG is enabled."""
    if settings.ollama_debug:
        logger.debug(" ".join(str(arg) for arg in args))


def _extract_text_from_part(part: dict) -> Optional[str]:
    """
    Extract meaningful text from Ollama chat API response JSON.
    
    Args:
        part: Dictionary containing model response data
        
    Returns:
        Extracted text string or None if no text found
    """
    # For /api/chat endpoint, check message.content first
    message = part.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str) and content != "":
            return content
    
    # Fallback to other possible fields
    for key in ("response", "response_text", "text", "output", "content"):
        v = part.get(key)
        if isinstance(v, str) and v != "":
            return v

    # Handle OpenAI-style response format (for compatibility)
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


def request_stream_sync(
        user_msg: str,
        model: Optional[str],
        on_chunk: Callable[[dict], None],
        conversation_history: Optional[list[dict]] = None
) -> None:
    """
    Core synchronous streaming function with conversation memory support.
    Uses Ollama's /api/chat endpoint with native message array support.
    
    Args:
        user_msg: The user's message to send to the model
        model: Model name to use (uses default if None)
        on_chunk: Callback function to handle response chunks
        conversation_history: Optional list of previous conversation messages
                            Format: [{"role": "user/assistant", "content": "..."}]
    """
    m = model or settings.ollama_model

    # Build messages array for /api/chat endpoint
    messages = []
    if conversation_history:
        messages.extend(conversation_history)
    messages.append({"role": "user", "content": user_msg})
    
    payload = {"model": m, "messages": messages, "stream": True}

    try:
        client = httpx.Client(
            timeout=httpx.Timeout(
                timeout=settings.request_timeout,
                connect=settings.connect_timeout
            )
        )
    except Exception as e:
        logger.error(f"Failed to create HTTP client: {e}")
        on_chunk({"type": "error", "error": f"建立 HTTP 客戶端失敗：{e}"})
        return

    try:
        with client.stream("POST", ENDPOINT, json=payload,
                           headers={"Accept": "text/event-stream, application/json"}) as resp:
            if resp.status_code != 200:
                error_msg = f"HTTP {resp.status_code}: {resp.text}"
                logger.error(f"Stream request failed: {error_msg}")
                on_chunk({"type": "error", "error": error_msg})
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
        logger.warning(f"Stream exception, falling back to non-streaming: {e}")

        try:
            resp2 = client.post(
                ENDPOINT,
                json={"model": m, "messages": messages, "stream": False},
                timeout=settings.request_timeout
            )
        except Exception as e2:
            error_msg = f"Ollama 連線失敗：{e2}"
            logger.error(error_msg)
            on_chunk({"type": "error", "error": error_msg})
            return

        if resp2.status_code != 200:
            error_msg = f"HTTP {resp2.status_code}: {resp2.text}"
            logger.error(error_msg)
            on_chunk({"type": "error", "error": error_msg})
            return

        try:
            j = resp2.json()
            # For /api/chat, response is in message.content
            message = j.get("message", {})
            text = message.get("content") if isinstance(message, dict) else None
            
            # Fallback to other fields
            if not text:
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

        # Send text in chunks
        for i in range(0, len(text), CHUNK_SIZE):
            on_chunk({"type": "delta", "text": text[i: i + CHUNK_SIZE]})
        on_chunk({"type": "done"})
        return
    finally:
        client.close()