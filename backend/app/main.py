"""
Main FastAPI application for CS_Agent backend.
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import httpx
import logging

from app.routers.ws import router as ws_router

from app.config import settings

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = FastAPI(title="CS_Agent_Backend_WS")

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    """
    Health check endpoint that verifies both the API and Ollama connection.
    
    Returns:
        dict: Status information including Ollama connectivity
    """
    try:
        # Check Ollama connection
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{settings.ollama_url}/api/tags")
            if response.status_code == 200:
                return {"status": "ok", "ollama": "connected"}
            else:
                return {"status": "degraded", "ollama": "error", "details": f"HTTP {response.status_code}"}
    except Exception as e:
        logger.warning(f"Health check - Ollama connection failed: {e}")
        return {"status": "degraded", "ollama": "disconnected", "error": str(e)}


# WebSocket 路由
app.include_router(ws_router)
