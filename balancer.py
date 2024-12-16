from fastapi import FastAPI, Request, Response
import uvicorn
import asyncio
import httpx
import logging

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
# List of worker endpoints
# This is example, you should replace with your worker URLs
worker_urls = [f"http://127.0.0.1:920{i}/prompt_sync" for i in range(1)]

# Initialize a queue with available workers
available_workers = asyncio.Queue()
for url in worker_urls:
    available_workers.put_nowait(url)

MAX_REQUEST_SIZE = 1024 * 1024  # We can limit the request size to 1MB

@app.post("/prompt")
async def prompt(request: Request):
    worker_url = await available_workers.get()
    try:
        data = await request.body()
        headers = dict(request.headers)

        # Remove headers that should not be forwarded
        headers.pop('host', None)
        headers.pop('content-length', None)

        async with httpx.AsyncClient() as client:
            response = await client.post(
                worker_url,
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
        # Log exceptions with client IP
        logger.error(
            f"Error processing request",
        )
        return Response(content="Internal Server Error", status_code=500)
    finally:
        await available_workers.put(worker_url)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9300)
    args = parser.parse_args()
    uvicorn.run(app, host="127.0.0.1", port=args.port)
