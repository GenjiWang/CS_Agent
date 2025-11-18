from fastapi import APIRouter, WebSocket, WebSocketDisconnect
import json, asyncio, threading
from ..services.streamer import request_stream_sync

router = APIRouter()

# 全局字典存儲每個連接的對話歷史
conversation_sessions = {}


def json_dumps(obj):
    return json.dumps(obj, ensure_ascii=False)


@router.websocket("/ws/chat")
async def ws_chat(websocket: WebSocket):
    """
    WebSocket endpoint for chat streaming with conversation memory.
    """
    await websocket.accept()
    loop = asyncio.get_event_loop()

    # 為此連接生成唯一 session_id
    session_id = id(websocket)
    conversation_sessions[session_id] = []

    try:
        while True:
            raw = await websocket.receive_text()

            try:
                payload = json.loads(raw)
            except Exception:
                await websocket.send_text(json_dumps({"type": "error", "error": "JSON 格式不正確"}))
                continue

            # 處理心跳
            if payload.get("type") == "ping":
                await websocket.send_text(json_dumps({"type": "pong"}))
                continue

            # 處理清除歷史指令
            if payload.get("type") == "clear_history":
                conversation_sessions[session_id] = []
                await websocket.send_text(json_dumps({"type": "history_cleared"}))
                continue

            messages = payload.get("messages", [])
            user_msg = next((m.get("content") for m in messages if m.get("role") == "user"), None)
            if not user_msg:
                await websocket.send_text(json_dumps({"type": "error", "error": "缺少使用者訊息"}))
                continue

            model = payload.get("model")

            # 獲取當前對話歷史
            history = conversation_sessions[session_id]

            q: asyncio.Queue = asyncio.Queue()

            # 用於收集助手回應
            assistant_response = []

            def on_chunk(chunk: dict):
                # 收集助手回應文本
                if chunk.get("type") == "delta":
                    assistant_response.append(chunk.get("text", ""))

                def _put():
                    q.put_nowait(chunk)

                loop.call_soon_threadsafe(_put)

            # 傳入對話歷史
            t = threading.Thread(
                target=request_stream_sync,
                args=(user_msg, model, on_chunk, history.copy()),
                daemon=True
            )
            t.start()

            while True:
                chunk = await q.get()
                await websocket.send_text(json_dumps(chunk))

                if chunk.get("type") in ("done", "error"):
                    # 對話完成後更新歷史
                    if chunk.get("type") == "done" and assistant_response:
                        history.append({"role": "user", "content": user_msg})
                        history.append({"role": "assistant", "content": "".join(assistant_response)})

                        # 限制歷史長度（保留最近 10 輪對話 = 20 條消息）
                        if len(history) > 20:
                            conversation_sessions[session_id] = history[-20:]
                    break

    except WebSocketDisconnect:
        # 清理此連接的對話歷史
        if session_id in conversation_sessions:
            del conversation_sessions[session_id]
