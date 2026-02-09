"""
Configuration management for CS_Agent backend.
Uses Pydantic Settings for environment-based configuration.
"""
from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    """Application settings with environment variable support."""
    
    # Ollama Configuration
    ollama_model: str = "gpt-oss:20b"
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_debug: bool = False
    
    # WebSocket Configuration
    max_message_size: int = 10 * 1024  # 10KB
    history_max_length: int = 20  # Maximum number of messages to keep in history
    
    # Timeout Configuration
    request_timeout: float = 30.0
    connect_timeout: float = 5.0
    
    # CORS Configuration
    cors_origins: list[str] = ["http://localhost:5173", "http://localhost:3000"]
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


# Global settings instance
settings = Settings()
