FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MCP_TRANSPORT=http \
    HOST=0.0.0.0 \
    PORT=3032 \
    PYTHONPATH=/app/src

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir \
      "httpx>=0.28.1" "mcp[cli]>=1.12.0" "pydantic>=2.11.3" \
      "python-dotenv>=1.1.0" "openai>=1.50.0" "uvicorn>=0.30.0" "starlette>=0.37.0"

EXPOSE 3032
CMD ["python", "src/server.py"]
