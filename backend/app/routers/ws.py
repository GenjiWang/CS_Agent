from fastapi import APIRouter, WebSocket, WebSocketDisconnect
import json, asyncio, threading
from ..services.streamer import request_stream_sync

router = APIRouter()

def json_dumps(obj):
    # 將 Python 物件序列化為 JSON 字串，確保非 ASCII 字元不會被 escape
    return json.dumps(obj, ensure_ascii=False)

@router.websocket("/ws/chat")
async def ws_chat(websocket: WebSocket):
    """
    WebSocket endpoint for chat streaming.
    設計要點：
      - 接受前端傳入的 JSON payload（含 messages、model 等）
      - 將使用者訊息交給 request_stream_sync（在背景 thread）處理
      - 使用 asyncio.Queue 作為 thread -> asyncio 事件迴圈的橋接
      - 只傳送三種格式給前端：{"type":"delta","text":...}, {"type":"done"}, {"type":"error", "error": ...}
    """
    await websocket.accept()  # 接受連線（完成 WebSocket 握手）
    loop = asyncio.get_event_loop()  # 取得當前事件迴圈，用以跨執行緒回推資料到 asyncio.Queue
    try:
        while True:
            # 等待並讀取前端傳來的一個文字訊息（blocking async）
            raw = await websocket.receive_text()

            # 解析 JSON payload，若非 JSON 則回傳錯誤給前端並繼續等待下一個訊息
            try:
                payload = json.loads(raw)
            except Exception:
                await websocket.send_text(json_dumps({"type":"error","error":"JSON 格式不正確"}))
                continue

            # 處理心跳 ping（前端可能以 type:'ping' 做保活）
            if payload.get("type") == "ping":
                await websocket.send_text(json_dumps({"type":"pong"}))
                continue

            # 從 payload.messages 中擷取第一個 role === 'user' 的 content 當作使用者訊息
            messages = payload.get("messages", [])
            user_msg = next((m.get("content") for m in messages if m.get("role") == "user"), None)
            if not user_msg:
                # 若沒有使用者訊息，回傳錯誤並繼續等待下一個 client message
                await websocket.send_text(json_dumps({"type":"error","error":"缺少使用者訊息"}))
                continue

            model = payload.get("model")  # 可為 None（request_stream_sync 會用預設 MODEL）

            # 建立 asyncio.Queue，作為 background thread -> async handler 的傳輸緩衝
            q: asyncio.Queue = asyncio.Queue()

            # 定義 on_chunk 回呼，供 request_stream_sync 在背景執行緒呼叫
            def on_chunk(chunk: dict):
                """
                這個函式在背景 thread 執行，目的是把 chunk 放入 asyncio.Queue。
                注意事項：
                  - 不能直接 await q.put，必須用 loop.call_soon_threadsafe 把同步操作排回事件迴圈。
                  - chunk 應該是簡潔格式：{"type":"delta","text":...} / {"type":"done"} / {"type":"error","error":...}
                """
                def _put():
                    # 這個函式會在事件迴圈中執行（非背景 thread），直接放入 queue
                    q.put_nowait(chunk)
                # 將 _put 排回事件迴圈執行（thread-safe）
                loop.call_soon_threadsafe(_put)

            # 在背景 thread 啟動同步的 streaming 呼叫，避免阻塞主事件迴圈
            t = threading.Thread(
                target=request_stream_sync,
                args=(user_msg, model, on_chunk),
                daemon=True
            )
            t.start()

            # 消費 queue，將 chunk 逐一發送回前端
            while True:
                chunk = await q.get()  # await 非阻塞地從事件迴圈中取得背景 thread 放入的 chunk
                await websocket.send_text(json_dumps(chunk))  # 直接把已序列化的 chunk 傳給前端

                # 如果收到結束或錯誤訊息，跳出內層迴圈，回到外層等待下一個 client payload
                if chunk.get("type") in ("done", "error"):
                    break

    except WebSocketDisconnect:
        # 客戶端關閉連線時會丟出 WebSocketDisconnect；在這裡可以做資源清理（如果需要）
        # 目前採取靜默處理，讓函式自然返回
        pass
