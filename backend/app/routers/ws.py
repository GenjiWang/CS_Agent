from fastapi import APIRouter, WebSocket, WebSocketDisconnect
import json
from ..services.streamer import stream_reply
from ..utils.jsonsafe import json_dumps

router = APIRouter()

@router.websocket("/ws/chat")
async def ws_chat(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            raw = await websocket.receive_text()
            print("收到前端訊息:", raw)

            # 解析 JSON
            try:
                payload = json.loads(raw)
            except Exception:
                await websocket.send_text(json_dumps({"type": "error", "error": "JSON 格式不正確"}))
                continue

            # 心跳：ping -> pong
            if payload.get("type") == "ping":
                await websocket.send_text(json_dumps({"type": "pong"}))
                continue

            # 取出使用者訊息（前端傳 messages 陣列）
            messages = payload.get("messages", [])
            user_msg = None
            for m in messages:
                if m.get("role") == "user":
                    user_msg = m.get("content")
                    break

            if not user_msg:
                await websocket.send_text(json_dumps({"type": "error", "error": "缺少使用者訊息"}))
                continue

            # 串流回覆（目前為模擬；之後可改接 Ollama）
            try:
                async for chunk in stream_reply(user_msg):
                    # chunk 可能是 {"type":"delta","text":...} 或 {"type":"done"}
                    await websocket.send_text(json_dumps(chunk))
            except Exception as e:
                await websocket.send_text(json_dumps({"type": "error", "error": f"傳送失敗：{str(e)}"}))

    except WebSocketDisconnect:
        print("WebSocket disconnected")
