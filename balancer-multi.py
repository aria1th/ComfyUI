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

# Custom logging filter to include client IP
class ContextFilter(logging.Filter):
    def filter(self, record):
        record.clientip = getattr(record, 'clientip', 'unknown')
        return True

logger.addFilter(ContextFilter())

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

TIMEOUT_SECONDS = 600
MAX_REQUEST_SIZE = 1024 * 1024  # Limit request size to 1MB

worker_endpoints_str = os.getenv("WORKER_ENDPOINTS", "http://comfyui-worker:9200/prompt_sync")
WORKER_ENDPOINTS = [endpoint.strip() for endpoint in worker_endpoints_str.split(",") if endpoint.strip()]

current_index = 0
index_lock = asyncio.Lock()

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

        # 3) Round-robin: pick the next endpoint
        global current_index
        async with index_lock:
            endpoint = WORKER_ENDPOINTS[current_index]
            current_index = (current_index + 1) % len(WORKER_ENDPOINTS)

        async with httpx.AsyncClient() as client:
            response = await client.post(
                endpoint,
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
