import asyncio

# 這個模組負責「產生串流內容」
# 目前用模擬分段；未來可改成呼叫 Ollama 並 yield 模型的 token/chunk

async def stream_reply(user_msg: str):
    """
    以 async generator 逐段輸出訊息。
    符合前端協議：delta/done。
    """
    chunks = ["你說：", user_msg, "。", "這是示範的串流回覆，", "稍後可改為模型輸出。"]
    for ch in chunks:
        yield {"type": "delta", "text": ch}
        # 用 asyncio.sleep 模擬串流延遲（不阻塞 event loop）
        await asyncio.sleep(0.05)
    yield {"type": "done"}
