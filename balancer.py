from fastapi import FastAPI, Request, Response
import uvicorn
import asyncio
import httpx
import logging
import os

# Set up logging
logger = logging.getLogger("Balancer")
logger.setLevel(logging.INFO)
handler = logging.FileHandler("./app.log")  # Specify your log file path
formatter = logging.Formatter(
    '%(asctime)s - %(levelname)s - %(clientip)s - "%(request_line)s" %(status_code)s'
)
handler.setFormatter(formatter)
logger.addHandler(handler)

app = FastAPI()

# Middleware to log each request
@app.middleware("http")
async def log_requests(request: Request, call_next):
    response = await call_next(request)
    # Log the request
    logger.info(
        '',
        extra={
            'clientip': request.client.host,
            'request_line': f"{request.method} {request.url.path} HTTP/{request.scope.get('http_version', '1.1')}",
            'status_code': response.status_code
        }
    )
    return response

# Custom logging filter to include client IP
class ContextFilter(logging.Filter):
    def filter(self, record):
        record.clientip = getattr(record, 'clientip', 'unknown')
        return True

logger.addFilter(ContextFilter())

TIMEOUT_SECONDS = 600

# The worker endpoint is a single endpoint representing the "service"
# Docker Compose load-balances requests to all replicas of that service.
WORKER_ENDPOINT = os.getenv("WORKER_ENDPOINT", "http://comfyui-worker:9200/prompt_sync")

MAX_REQUEST_SIZE = 1024 * 1024  # Limit request size to 1MB

@app.post("/prompt")
async def prompt(request: Request):
    try:
        data = await request.body()

        # Limit request size
        if len(data) > MAX_REQUEST_SIZE:
            return Response(content="Request too large", status_code=413)

        headers = dict(request.headers)
        # Remove headers that should not be forwarded
        headers.pop('host', None)
        headers.pop('content-length', None)

        async with httpx.AsyncClient() as client:
            response = await client.post(
                WORKER_ENDPOINT,
                content=data,
                headers=headers,
                timeout=TIMEOUT_SECONDS
            )

        return Response(
            content=response.content,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.headers.get('content-type')
        )
    except Exception as e:
        logger.error("Error processing request", exc_info=e)
        return Response(content="Internal Server Error", status_code=500)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args()
    uvicorn.run(app, host="0.0.0.0", port=args.port)
