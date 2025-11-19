# CS_Agent Backend

FastAPI-based backend service for the CS_Agent intelligent chat bot with streaming support and conversation memory.

## Features

- **WebSocket streaming**: Real-time streaming responses from Ollama
- **Conversation memory**: Maintains conversation history per session
- **Configuration management**: Environment-based configuration using Pydantic Settings
- **Security**: Request size limits, CORS, timeout configuration
- **Session management**: Automatic TTL-based session cleanup to prevent memory leaks
- **Health monitoring**: Health check endpoint with Ollama connectivity verification
- **Logging**: Comprehensive structured logging

## Requirements

- Python 3.10+
- Ollama server running (default: http://127.0.0.1:8008)

## Installation

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Configure environment variables (optional):
```bash
cp .env.example .env
# Edit .env with your configuration
```

## Configuration

Configuration can be set via environment variables or `.env` file:

| Variable | Default | Description |
|----------|---------|-------------|
| `OLLAMA_MODEL` | `gpt-oss:20b` | Model name to use |
| `OLLAMA_URL` | `http://127.0.0.1:8008` | Ollama server URL |
| `OLLAMA_DEBUG` | `0` | Enable debug logging (0 or 1) |
| `MAX_MESSAGE_SIZE` | `10240` | Maximum message size in bytes |
| `HISTORY_MAX_LENGTH` | `20` | Maximum conversation history length |
| `REQUEST_TIMEOUT` | `30.0` | Request timeout in seconds |
| `CONNECT_TIMEOUT` | `5.0` | Connection timeout in seconds |
| `CORS_ORIGINS` | `http://localhost:5173,http://localhost:3000` | Allowed CORS origins |

## Running

### Development
```bash
cd backend
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### Production
```bash
cd backend
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
```

## API Endpoints

### Health Check
```
GET /health
```
Returns server and Ollama connectivity status.

### WebSocket Chat
```
WS /ws/chat
```
WebSocket endpoint for streaming chat with conversation memory.

#### Message Format

**Client Messages:**
```json
{
  "messages": [
    {"role": "user", "content": "Hello"}
  ],
  "model": "gpt-oss:20b"
}
```

**Heartbeat:**
```json
{"type": "ping"}
```

**Clear History:**
```json
{"type": "clear_history"}
```

**Server Responses:**
```json
{"type": "delta", "text": "streaming text..."}
{"type": "done"}
{"type": "error", "error": "error message"}
{"type": "pong"}
{"type": "history_cleared"}
```

## Architecture

```
backend/
├── app/
│   ├── main.py           # FastAPI application entry point
│   ├── config.py         # Configuration management
│   ├── routers/
│   │   └── ws.py         # WebSocket router
│   ├── services/
│   │   └── streamer.py   # Ollama streaming service
│   └── utils/
│       └── jsonsafe.py   # JSON utilities
├── requirements.txt      # Python dependencies
└── .env.example         # Example environment configuration
```

## Security Features

1. **Request Size Validation**: Limits message size to prevent DoS attacks
2. **Session TTL**: Automatic cleanup of inactive sessions after 1 hour
3. **Timeout Configuration**: Configurable timeouts for all HTTP requests
4. **CORS Protection**: Configurable allowed origins
5. **Error Handling**: Comprehensive error handling and logging

## Logging

The application uses Python's standard logging module with structured logging:
- INFO level: Normal operations (connections, completions)
- WARNING level: Recoverable issues (invalid input, timeouts)
- ERROR level: Serious errors (connection failures, exceptions)

## Testing

A simple test script is provided:
```bash
python test.py
```

## Development Notes

- The application uses asyncio for concurrent request handling
- Uses Ollama's `/api/chat` endpoint for native message array support
- Ollama streaming responses are handled in thread pools to avoid blocking
- Session management uses TTL cache to prevent memory leaks
- All configuration is centralized in `config.py` for easy testing and deployment
