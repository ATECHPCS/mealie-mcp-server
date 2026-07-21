import atexit
import logging
import os
import traceback

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from mealie import MealieFetcher
from prompts import register_prompts
from tools import register_all_tools

load_dotenv()

log_level_name = os.getenv("LOG_LEVEL", "INFO")
log_level = getattr(logging, log_level_name.upper(), logging.INFO)
logging.basicConfig(
    level=log_level,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("mealie-mcp")

mcp = FastMCP("mealie")

MEALIE_BASE_URL = os.getenv("MEALIE_BASE_URL")
MEALIE_API_KEY = os.getenv("MEALIE_API_KEY")
if not MEALIE_BASE_URL or not MEALIE_API_KEY:
    raise ValueError(
        "MEALIE_BASE_URL and MEALIE_API_KEY must be set in environment variables."
    )

try:
    mealie = MealieFetcher(base_url=MEALIE_BASE_URL, api_key=MEALIE_API_KEY)
except Exception as e:
    logger.error({"message": "Failed to initialize Mealie client", "error": str(e)})
    logger.debug({"message": "Error traceback", "traceback": traceback.format_exc()})
    raise

atexit.register(mealie.close)

register_prompts(mcp)
register_all_tools(mcp, mealie)


def main():
    transport = os.getenv("MCP_TRANSPORT", "stdio").lower()
    try:
        if transport in ("http", "streamable-http", "streamable_http"):
            import uvicorn
            from starlette.middleware.base import BaseHTTPMiddleware
            from starlette.requests import Request
            from starlette.responses import JSONResponse
            from starlette.routing import Route

            host = os.getenv("HOST", "0.0.0.0")
            port = int(os.getenv("PORT", "3032"))
            mcp.settings.host = host
            mcp.settings.port = port

            app = mcp.streamable_http_app()

            async def _health(_request: Request):
                return JSONResponse({"status": "ok", "service": "mealie-mcp"})

            app.router.routes.insert(0, Route("/health", _health, methods=["GET"]))

            keys = {k.strip() for k in os.getenv("MCP_API_KEYS", "").split(",") if k.strip()}
            if os.getenv("MCP_AUTH", "").lower() == "api_key" and keys:
                header = os.getenv("MCP_API_HEADER", "X-API-Key")

                class ApiKeyMiddleware(BaseHTTPMiddleware):
                    async def dispatch(self, request: Request, call_next):
                        if request.url.path == "/health":
                            return await call_next(request)
                        if request.headers.get(header) not in keys:
                            return JSONResponse({"error": "unauthorized"}, status_code=401)
                        return await call_next(request)

                app.add_middleware(ApiKeyMiddleware)
                logger.info({"message": "Inbound API-key auth enabled", "header": header})

            logger.info({"message": "Starting Mealie MCP Server (streamable-http)", "host": host, "port": port})
            uvicorn.run(app, host=host, port=port)
        else:
            logger.info({"message": "Starting Mealie MCP Server (stdio)"})
            mcp.run(transport="stdio")
    except Exception as e:
        logger.critical({"message": "Fatal error in Mealie MCP Server", "error": str(e)})
        logger.debug({"message": "Error traceback", "traceback": traceback.format_exc()})
        raise


if __name__ == "__main__":
    main()
