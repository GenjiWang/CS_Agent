from fastapi import FastAPI
from .routers.ws import router as ws_router

app = FastAPI(title="CS_Agent_Backend_WS")

@app.get("/health")
def health():
    return {"status": "ok"}

# WebSocket 路由
app.include_router(ws_router)
