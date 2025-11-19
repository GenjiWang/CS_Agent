"""
WebSocket router for real-time chat with conversation memory.
Handles streaming responses from Ollama and maintains conversation history.
"""
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
import json
import asyncio
import logging
from typing import Dict, List
from cachetools import TTLCache

from ..services.streamer import request_stream_sync
from ..utils.jsonsafe import json_dumps
from ..config import settings

router = APIRouter()

# Configure logger
logger = logging.getLogger(__name__)

# Use TTL cache to prevent memory leaks (sessions expire after 1 hour)
conversation_sessions: TTLCache = TTLCache(maxsize=1000, ttl=3600)


@router.websocket("/ws/chat")
async def ws_chat(websocket: WebSocket) -> None:
    """
    WebSocket endpoint for chat streaming with conversation memory.
    
    Features:
    - Streaming responses from Ollama
    - Conversation history management
    - Heartbeat support
    - Request size validation
    - Automatic session cleanup
    """
    await websocket.accept()
    loop = asyncio.get_event_loop()

    # Generate unique session_id for this connection
    session_id = id(websocket)
    conversation_sessions[session_id] = []
    
    logger.info(f"WebSocket connection established: session_id={session_id}")

    try:
        while True:
            raw = await websocket.receive_text()

            # Validate message size to prevent DoS attacks
            if len(raw) > settings.max_message_size:
                await websocket.send_text(json_dumps({
                    "type": "error",
                    "error": f"訊息過大 (最大 {settings.max_message_size} bytes)"
                }))
                logger.warning(f"Message too large: {len(raw)} bytes from session {session_id}")
                continue

            try:
                payload = json.loads(raw)
            except Exception as e:
                await websocket.send_text(json_dumps({"type": "error", "error": "JSON 格式不正確"}))
                logger.warning(f"Invalid JSON from session {session_id}: {e}")
                continue

            # Handle heartbeat
            if payload.get("type") == "ping":
                await websocket.send_text(json_dumps({"type": "pong"}))
                continue

            # Handle clear history command
            if payload.get("type") == "clear_history":
                conversation_sessions[session_id] = []
                await websocket.send_text(json_dumps({"type": "history_cleared"}))
                logger.info(f"History cleared for session {session_id}")
                continue

            messages = payload.get("messages", [])
            user_msg = next((m.get("content") for m in messages if m.get("role") == "user"), None)
            if not user_msg:
                await websocket.send_text(json_dumps({"type": "error", "error": "缺少使用者訊息"}))
                continue

            model = payload.get("model")

            # Get current conversation history
            history: List[Dict[str, str]] = conversation_sessions[session_id]

            q: asyncio.Queue = asyncio.Queue()

            # Collect assistant response
            assistant_response: List[str] = []

            def on_chunk(chunk: dict) -> None:
                """Callback to handle response chunks from the streaming service."""
                # Collect assistant response text
                if chunk.get("type") == "delta":
                    assistant_response.append(chunk.get("text", ""))

                def _put() -> None:
                    try:
                        q.put_nowait(chunk)
                    except asyncio.QueueFull:
                        logger.warning(f"Queue full for session {session_id}")

                loop.call_soon_threadsafe(_put)

            # Use asyncio task instead of threading for better resource management
            task = loop.run_in_executor(
                None,
                request_stream_sync,
                user_msg,
                model,
                on_chunk,
                history.copy()
            )

            try:
                while True:
                    chunk = await q.get()
                    await websocket.send_text(json_dumps(chunk))

                    if chunk.get("type") in ("done", "error"):
                        # Update history after conversation completes
                        if chunk.get("type") == "done" and assistant_response:
                            history.append({"role": "user", "content": user_msg})
                            history.append({"role": "assistant", "content": "".join(assistant_response)})

                            # Limit history length (keep most recent messages)
                            if len(history) > settings.history_max_length:
                                conversation_sessions[session_id] = history[-settings.history_max_length:]
                            
                            logger.info(f"Conversation completed for session {session_id}, history size: {len(history)}")
                        break

            except Exception as e:
                logger.error(f"Error processing response for session {session_id}: {e}")
                await websocket.send_text(json_dumps({
                    "type": "error",
                    "error": "處理回應時發生錯誤"
                }))
            finally:
                # Ensure task is completed
                await task

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected: session_id={session_id}")
    except Exception as e:
        logger.error(f"Unexpected error in WebSocket handler for session {session_id}: {e}")
    finally:
        # Cleanup conversation history for this connection
        if session_id in conversation_sessions:
            del conversation_sessions[session_id]
            logger.info(f"Session cleaned up: session_id={session_id}")
