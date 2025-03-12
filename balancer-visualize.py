import os
import uuid
import json
import logging
import asyncio
import httpx
import ssl
import threading
from collections import defaultdict
from typing import Optional
from fastapi import FastAPI, Request, Response, BackgroundTasks
from fastapi.responses import HTMLResponse
import uvicorn
import websocket  # from "websocket-client" package

# ==========================
# CONFIGURATIONS & LOGGING
# ==========================

logger = logging.getLogger("Balancer")
logger.setLevel(logging.DEBUG)
handler = logging.FileHandler("./balancer.log")  # Specify your log file path
formatter = logging.Formatter(
    '%(asctime)s - %(levelname)s - %(clientip)s - "%(request_line)s" %(status_code)s %(message)s'
)
handler.setFormatter(formatter)
logger.addHandler(handler)

class ContextFilter(logging.Filter):
    def filter(self, record):
        # Provide default values for any missing fields in the LogRecord
        record.clientip = getattr(record, "clientip", "unknown")
        return True

logger.addFilter(ContextFilter())


def default_logger_info(msg, extra=None):
    """
    Helper to log with the default logger.
    """
    logger.info(
        msg,
        extra={"clientip": "N/A", "request_line": "N/A", "status_code": 200} if extra is None else extra
    )


# ==========================
# FASTAPI APP
# ==========================

app = FastAPI()

@app.middleware("http")
async def log_requests(request: Request, call_next):
    """
    Middleware to log each incoming request.
    """
    response = await call_next(request)
    logger.info(
        "",
        extra={
            "clientip": request.client.host,
            "request_line": f"{request.method} {request.url.path} HTTP/{request.scope.get('http_version', '1.1')}",
            "status_code": response.status_code,
        },
    )
    return response

def get_extra_from_request(request: Request, status_code: int):
    """
    Helper for constructing log 'extra' dict for consistent logging.
    """
    return {
        "clientip": request.client.host,
        "request_line": f"{request.method} {request.url.path} HTTP/{request.scope.get('http_version', '1.1')}",
        "status_code": (status_code if status_code else 301),
    }


# ==========================
# BALANCER / WORKERS SETUP
# ==========================

TIMEOUT_SECONDS = 600
MAX_REQUEST_SIZE = 30 * 1024 * 1024  # 30 MB

# Worker endpoints (comma-separated via ENV or default):
worker_endpoints_str = os.getenv(
    "WORKER_ENDPOINTS",
    ""
)

WORKER_ENDPOINTS = [
    endpoint.strip() for endpoint in worker_endpoints_str.split(",") if endpoint.strip()
]

# Concurrency limit per worker:
CONCURRENCY_LIMIT = 2

# We keep a semaphore per worker to manage concurrency
WORKER_SEMAPHORES = [asyncio.Semaphore(CONCURRENCY_LIMIT) for _ in WORKER_ENDPOINTS]

# Round-robin index lock
current_index = 0
index_lock = asyncio.Lock()

# A simple in-memory store for progress logs keyed by prompt_id
PROMPT_PROGRESS_LOGS = defaultdict(list)

# A dictionary to track which worker is processing each prompt_id:
# { prompt_id: { "worker_index": i, "status": "running" or "done"/"error", ... } }
ACTIVE_CONNECTIONS = {}

# A global queue for requests that cannot find a suitable worker right away
PENDING_QUEUE = asyncio.Queue()

# For thread-safe access to PROMPT_PROGRESS_LOGS and ACTIVE_CONNECTIONS, we use a lock:
progress_data_lock = threading.Lock()


# ==========================
# WEBSOCKET PROGRESS (Blocking => Thread)
# ==========================

def track_worker_progress_ws_blocking(ws_url: str, client_id: str, prompt_id: str):
    """
    Connects via WebSocket using the *blocking* websocket-client library,
    listens for progress messages, logs them, and updates data structures
    in a thread-safe manner.
    """
    logger.info(
        f"[WS] Connecting in a dedicated thread to {ws_url}",
        extra={"clientip": "N/A", "request_line": "WS-Connect", "status_code": 101}
    )

    ws = websocket.WebSocket()
    try:
        ws.connect(ws_url)
        finished_nodes = set()
        while True:
            msg = ws.recv()  # blocking read
            if not isinstance(msg, str):
                logger.error(
                    f"[WS] Non-string message received: {msg}",
                    extra={"clientip": "N/A", "request_line": "WS-Message", "status_code": 200}
                )
                continue

            payload = json.loads(msg)
            msg_type = payload.get("type")
            logger.debug(
                f"[WS] {prompt_id} -> {msg_type}: {payload}",
                extra={"clientip": "N/A", "request_line": "WS-Message", "status_code": 200}
            )

            if msg_type == "progress":
                data = payload["data"]
                current_step = data["value"]
                max_step = data["max"]
                default_logger_info(f"[WS] {prompt_id} => Step {current_step} / {max_step}")
                with progress_data_lock:
                    PROMPT_PROGRESS_LOGS[prompt_id].append(f"Step: {current_step} / {max_step}")

            elif msg_type == "execution_cached":
                data = payload["data"]
                for node in data["nodes"]:
                    if node not in finished_nodes:
                        finished_nodes.add(node)
                        default_logger_info(f"[WS] {prompt_id} => Cached node finished: {node}")
                        with progress_data_lock:
                            PROMPT_PROGRESS_LOGS[prompt_id].append(f"Cached node: {node}")

            elif msg_type == "executing":
                data = payload["data"]
                node_id = data["node"]
                if node_id and node_id not in finished_nodes:
                    finished_nodes.add(node_id)
                    default_logger_info(f"[WS] {prompt_id} => Node finished: {node_id}")
                    with progress_data_lock:
                        PROMPT_PROGRESS_LOGS[prompt_id].append(f"Finished node: {node_id}")

                # If data['node'] is None AND data['prompt_id'] == prompt_id => done
                if node_id is None and data.get("prompt_id") == prompt_id:
                    default_logger_info(f"[WS] {prompt_id} => Generation completed.")
                    with progress_data_lock:
                        PROMPT_PROGRESS_LOGS[prompt_id].append("Done")
                        if prompt_id in ACTIVE_CONNECTIONS:
                            ACTIVE_CONNECTIONS[prompt_id]["status"] = "done"
                    break
            elif msg_type == "status":
                # mark as done
                default_logger_info(f"[WS] {prompt_id} => Status message: {payload}")
                with progress_data_lock:
                    if prompt_id in ACTIVE_CONNECTIONS:
                        ACTIVE_CONNECTIONS[prompt_id]["status"] = "done"
            else:
                logger.debug(
                    f"[WS] Ignoring message type: {msg_type} with {payload}",
                    extra={"clientip": "N/A", "request_line": "WS-Message", "status_code": 200}
                )
                continue

    except Exception as wsex:
        logger.error(
            f"[WS] Error in progress loop for prompt {prompt_id}: {wsex}",
            extra={"clientip": "N/A", "request_line": "WS-Error", "status_code": 200}
        )
        # Mark prompt as "error" if not done
        with progress_data_lock:
            if prompt_id in ACTIVE_CONNECTIONS:
                current_status = ACTIVE_CONNECTIONS[prompt_id].get("status")
                if current_status not in ("done", "error"):
                    ACTIVE_CONNECTIONS[prompt_id]["status"] = "error"
    finally:
        ws.close()
        logger.info(
            f"[WS] Connection closed for prompt_id={prompt_id}",
            extra={"clientip": "N/A", "request_line": "WS-Close", "status_code": 200}
        )
        # If we never got node=None or ended early, mark done if not error:
        with progress_data_lock:
            if prompt_id in ACTIVE_CONNECTIONS:
                current_status = ACTIVE_CONNECTIONS[prompt_id].get("status")
                if current_status not in ("done", "error"):
                    ACTIVE_CONNECTIONS[prompt_id]["status"] = "done"
            else:
                ACTIVE_CONNECTIONS[prompt_id] = {
                    "worker_index": "unknown",
                    "status": "done (no active connection)",
                }


def launch_progress_thread(ws_url: str, client_id: str, prompt_id: str):
    """
    Spawns a dedicated thread to run the blocking WebSocket progress function.
    """
    thread = threading.Thread(
        target=track_worker_progress_ws_blocking,
        args=(ws_url, client_id, prompt_id),
        daemon=True,
    )
    thread.start()


# ==========================
# WORKER PICKING LOGIC
# ==========================

def worker_can_accept(i: int) -> bool:
    """
    Returns True if worker i can accept a new request:
      - If concurrency_in_use < CONCURRENCY_LIMIT
        OR concurrency_in_use == CONCURRENCY_LIMIT but requests_waiting < 1
    """
    concurrency_in_use = CONCURRENCY_LIMIT - WORKER_SEMAPHORES[i]._value
    requests_waiting = len(WORKER_SEMAPHORES[i]._waiters) if WORKER_SEMAPHORES[i]._waiters else 0

    # If concurrency usage is not maxed out, or if it's exactly max but queue is empty
    if concurrency_in_use < CONCURRENCY_LIMIT:
        return True
    elif concurrency_in_use == CONCURRENCY_LIMIT and requests_waiting < 1:
        return True
    return False

async def pick_worker_round_robin() -> Optional[int]:
    """
    Round-robin attempt to find a worker that can accept a request.
    Returns the worker index if found, else None.
    """
    global current_index
    async with index_lock:
        for _ in range(len(WORKER_ENDPOINTS)):
            i = current_index
            current_index = (current_index + 1) % len(WORKER_ENDPOINTS)
            if worker_can_accept(i):
                return i
    return None


async def handle_request_on_worker(
    worker_index: int,
    request_body: bytes,
    headers: dict,
    prompt_id_placeholder: str,
    request_obj: Request
) -> Response:
    """
    Submits the request to the chosen worker under a concurrency semaphore,
    launches a thread to track progress, and returns the worker's response.
    """
    async with WORKER_SEMAPHORES[worker_index]:
        endpoint = WORKER_ENDPOINTS[worker_index]
        logger.info(
            f"Forwarding request to worker {worker_index}: {endpoint}",
            extra=get_extra_from_request(request_obj, 200),
        )

        async with httpx.AsyncClient() as client:
            response = await client.post(
                endpoint, content=request_body, headers=headers, timeout=TIMEOUT_SECONDS
            )

        # Try to parse out prompt_id
        prompt_id = None
        try:
            resp_json = response.json()
            prompt_id = resp_json.get("prompt_id")
        except:
            logger.error(
                f"Error parsing response JSON from worker: {response.content}",
                extra=get_extra_from_request(request_obj, 500)
            )

        # If we got a prompt_id, track it:
        if prompt_id:
            with progress_data_lock:
                if prompt_id in ACTIVE_CONNECTIONS:
                    logger.warning(
                        f"Duplicate prompt_id in ACTIVE_CONNECTIONS: {prompt_id}",
                        extra=get_extra_from_request(request_obj, 500)
                    )
                ACTIVE_CONNECTIONS[prompt_id] = {
                    "worker_index": worker_index,
                    "status": "running",
                }

            # Build the WS URL (assuming ComfyUI's ws://host:port/ws?clientId=...)
            base_ws = endpoint.replace("http://", "ws://").replace("/prompt_sync", "")
            client_id = str(uuid.uuid4())
            ws_url = f"{base_ws}/ws?clientId={client_id}"

            # Launch a separate thread to track progress
            launch_progress_thread(ws_url, client_id, prompt_id)

        else:
            # If no prompt_id was provided, store a placeholder ID for logs
            prompt_id = prompt_id_placeholder
            logger.warning(
                f"No real prompt_id found in worker response: {response.content}",
                extra=get_extra_from_request(request_obj, 200)
            )
            with progress_data_lock:
                ACTIVE_CONNECTIONS[prompt_id] = {
                    "worker_index": worker_index,
                    "status": "running (no real prompt_id)",
                }

        # Return the raw response from the worker
        return Response(
            content=response.content,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.headers.get("content-type"),
        )


async def dispatch_pending_requests():
    """
    Drains the global PENDING_QUEUE if any worker becomes available.
    """
    while True:
        try:
            request_item = PENDING_QUEUE.get_nowait()
        except asyncio.QueueEmpty:
            break

        worker_index = await pick_worker_round_robin()
        if worker_index is not None:
            request_item["worker_index"] = worker_index
            request_item["event"].set()
        else:
            # No worker is available yet—put it back & break
            PENDING_QUEUE.put_nowait(request_item)
            break


# ==========================
# MAIN PROMPT ENDPOINT
# ==========================

@app.post("/prompt")
async def prompt(request: Request, background_tasks: BackgroundTasks):
    """
    Receives an image/prompt-generation request, picks a worker if available,
    or queues it if all are at capacity+1. Returns the worker's response eventually.
    """
    try:
        data = await request.body()
        if len(data) > MAX_REQUEST_SIZE:
            return Response(content="Request too large", status_code=413)

        headers = dict(request.headers)
        headers.pop("host", None)
        headers.pop("content-length", None)

        worker_index = await pick_worker_round_robin()
        if worker_index is not None:
            resp = await handle_request_on_worker(
                worker_index,
                data,
                headers,
                f"temp_{uuid.uuid4()}",
                request
            )
            return resp
        else:
            # No immediate worker => place into queue
            default_logger_info("All workers at capacity+1; queueing request.")
            item_event = asyncio.Event()
            queue_item = {
                "request_body": data,
                "headers": headers,
                "prompt_id_placeholder": f"queued_{uuid.uuid4()}",
                "request_obj": request,
                "event": item_event,
                "worker_index": None,
            }
            await PENDING_QUEUE.put(queue_item)
            await item_event.wait()  # Wait until a worker is free

            assigned_worker = queue_item["worker_index"]
            resp = await handle_request_on_worker(
                assigned_worker,
                queue_item["request_body"],
                queue_item["headers"],
                queue_item["prompt_id_placeholder"],
                queue_item["request_obj"],
            )
            return resp

    except Exception as e:
        err_message = f"Error processing request in /prompt: {str(e)}"
        logger.error(err_message, extra=get_extra_from_request(request, 500))
        return Response(content="Internal Server Error", status_code=500)
    finally:
        # Always attempt to dispatch pending requests if concurrency freed up
        await dispatch_pending_requests()


# ==========================
# STATUS ENDPOINTS
# ==========================

@app.get("/status")
async def status():
    """
    Returns a JSON representation of each worker's concurrency usage
    plus details on active jobs and the global queue length.
    """
    status_info = []
    for i, endpoint in enumerate(WORKER_ENDPOINTS):
        concurrency_in_use = CONCURRENCY_LIMIT - WORKER_SEMAPHORES[i]._value
        requests_waiting = len(WORKER_SEMAPHORES[i]._waiters) if WORKER_SEMAPHORES[i]._waiters else 0
        worker_status = "idle" if concurrency_in_use == 0 else "processing"
        status_info.append({
            "worker_index": i,
            "endpoint": endpoint,
            "status": worker_status,
            "concurrency_in_use": concurrency_in_use,
            "requests_waiting": requests_waiting
        })

    with progress_data_lock:
        active_jobs_list = [
            {
                "prompt_id": pid,
                "worker_index": info["worker_index"],
                "status": info["status"]
            }
            for pid, info in ACTIVE_CONNECTIONS.items()
        ]

    return {
        "workers": status_info,
        "active_jobs": active_jobs_list,
        "pending_queue_length": PENDING_QUEUE.qsize(),
        "known_prompts_progress": list(PROMPT_PROGRESS_LOGS.keys()),
    }


@app.get("/progress/{prompt_id}")
async def get_progress(prompt_id: str):
    """
    Return the known progress logs for a given prompt_id (if any).
    """
    with progress_data_lock:
        logs = PROMPT_PROGRESS_LOGS.get(prompt_id, [])
        return {"prompt_id": prompt_id, "progress": logs}


@app.get("/status_page", response_class=HTMLResponse)
async def status_page():
    """
    Returns an HTML page that periodically fetches /status JSON (and optional /progress logs).
    This is a simple dashboard to see concurrency usage and active jobs.
    """
    global CONCURRENCY_LIMIT
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Worker Status</title>
        <!-- Bootstrap CSS from CDN -->
        <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" />
        <style>
            .status-idle {{
                color: green; 
                font-weight: bold; 
            }}
            .status-processing {{
                color: red; 
                font-weight: bold; 
            }}
            .progress-usage {{
                min-width: 120px;
            }}
            .log-area {{
                white-space: pre-wrap;
                font-family: monospace;
                background: #f8f9fa;
                padding: 10px;
                border: 1px solid #ccc;
                border-radius: 5px;
                max-height: 300px;
                overflow-y: auto;
            }}
            .table-hover tbody tr:hover td {{
                background-color: #f4f4f4;
            }}
        </style>
    </head>
    <body class="bg-light">
    <div class="container my-4">
        <h1 class="mb-4">Worker Status</h1>

        <div class="row mb-3">
            <div class="col-sm-4">
                <div><strong>Total Concurrency Usage:</strong> 
                    <span id="totalUsage">0</span> / {CONCURRENCY_LIMIT * len(WORKER_ENDPOINTS)}
                </div>
                <div><strong>Workers:</strong> 
                    <span id="workerCount">{len(WORKER_ENDPOINTS)}</span>
                </div>
                <div><strong>Global Queue Length:</strong> 
                    <span id="globalQueue">0</span>
                </div>
            </div>
            <div class="col-sm-8 d-flex align-items-center">
                <button class="btn btn-primary me-3" id="refreshBtn">Refresh Now</button>
                <div class="form-check">
                  <input class="form-check-input" type="checkbox" id="autoRefresh" checked>
                  <label class="form-check-label" for="autoRefresh">
                    Auto-Refresh (every <span id="refreshIntervalValue">5</span>s)
                  </label>
                </div>
            </div>
        </div>

        <table class="table table-bordered table-hover align-middle" id="statusTable">
            <thead class="table-secondary">
                <tr>
                    <th>Worker Index</th>
                    <th>Endpoint</th>
                    <th>Status</th>
                    <th>Concurrency Usage</th>
                    <th>Requests Waiting</th>
                </tr>
            </thead>
            <tbody>
            </tbody>
        </table>

        <p class="text-muted" id="lastUpdated">Last updated: --</p>

        <hr class="my-4">

        <h3>Active Jobs</h3>
        <div id="activeJobsDiv" class="mb-4">(none)</div>

        <hr class="my-4">

        <h3>Prompt Progress Logs</h3>
        <div class="mb-3">
            <label for="promptIdInput" class="form-label">
                Enter a <code>prompt_id</code> to see logs:
            </label>
            <div class="input-group" style="max-width: 400px;">
                <input type="text" id="promptIdInput" class="form-control" placeholder="prompt_id" />
                <button class="btn btn-success" id="fetchLogsBtn">Fetch Logs</button>
            </div>
        </div>
        <div id="progressLogs" class="log-area">(no logs)</div>
    </div>  <!-- /container -->

    <!-- Bootstrap JS (optional) + minimal custom JS for status updates -->
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    <script>
        const refreshIntervalMs = 5000;
        let autoRefreshEnabled = true;
        let refreshTimer = null;

        async function fetchStatus() {{
            try {{
                const response = await fetch('/status');
                const data = await response.json();
                
                const tbody = document.querySelector("#statusTable tbody");
                tbody.innerHTML = "";

                let totalUsage = 0;
                const concurrencyLimit = {CONCURRENCY_LIMIT};

                data.workers.forEach((item) => {{
                    const row = document.createElement("tr");

                    // Worker Index
                    const cellIndex = document.createElement("td");
                    cellIndex.textContent = item.worker_index;
                    
                    // Endpoint
                    const cellEndpoint = document.createElement("td");
                    cellEndpoint.textContent = item.endpoint;

                    // Status
                    const cellStatus = document.createElement("td");
                    cellStatus.textContent = item.status;
                    cellStatus.classList.add(
                        item.status === "idle" ? "status-idle" : "status-processing"
                    );

                    // Concurrency usage (progress bar)
                    const cellConcurrency = document.createElement("td");
                    cellConcurrency.className = "progress-usage";

                    const usageRatio = (item.concurrency_in_use / concurrencyLimit);
                    const usagePercent = (usageRatio * 100).toFixed(0) + "%";

                    const progressDiv = document.createElement("div");
                    progressDiv.className = "progress";

                    const progressBar = document.createElement("div");
                    progressBar.className = "progress-bar";
                    progressBar.style.width = usagePercent;
                    progressBar.textContent = item.concurrency_in_use + " / " + concurrencyLimit;

                    if (usageRatio < 0.5) {{
                        progressBar.classList.add("bg-success");
                    }} else if (usageRatio < 0.8) {{
                        progressBar.classList.add("bg-warning");
                    }} else {{
                        progressBar.classList.add("bg-danger");
                    }}

                    progressDiv.appendChild(progressBar);
                    cellConcurrency.appendChild(progressDiv);

                    // Requests waiting
                    const cellWaiting = document.createElement("td");
                    cellWaiting.textContent = item.requests_waiting;

                    row.appendChild(cellIndex);
                    row.appendChild(cellEndpoint);
                    row.appendChild(cellStatus);
                    row.appendChild(cellConcurrency);
                    row.appendChild(cellWaiting);

                    tbody.appendChild(row);

                    totalUsage += item.concurrency_in_use;
                }});

                document.getElementById("totalUsage").textContent = totalUsage;
                document.getElementById("globalQueue").textContent = data.pending_queue_length || 0;

                // Update last-updated timestamp
                const now = new Date();
                document.getElementById("lastUpdated").textContent = "Last updated: " + now.toLocaleTimeString();

                // Active jobs
                let activeJobsHtml = "";
                data.active_jobs.forEach((job) => {{
                    activeJobsHtml += `
                        <div class="mb-1">
                          <span class="fw-bold">Prompt ID:</span> <code>${{job.prompt_id}}</code> 
                          | Worker: ${{job.worker_index}} 
                          | Status: <span class="text-secondary">${{job.status}}</span>
                        </div>
                    `;
                }});
                if (activeJobsHtml === "") {{
                    activeJobsHtml = "(none)";
                }}
                document.getElementById("activeJobsDiv").innerHTML = activeJobsHtml;

            }} catch (e) {{
                console.error("Error fetching status:", e);
            }}
        }}

        function setupAutoRefresh() {{
            const checkbox = document.getElementById("autoRefresh");
            const refreshBtn = document.getElementById("refreshBtn");

            checkbox.addEventListener("change", function() {{
                autoRefreshEnabled = this.checked;
                if (autoRefreshEnabled) {{
                    startAutoRefresh();
                }} else {{
                    stopAutoRefresh();
                }}
            }});

            refreshBtn.addEventListener("click", function() {{
                fetchStatus();
            }});

            startAutoRefresh();
        }}

        function startAutoRefresh() {{
            if (refreshTimer) return;
            fetchStatus();
            refreshTimer = setInterval(() => {{
                if (autoRefreshEnabled) {{
                    fetchStatus();
                }}
            }}, refreshIntervalMs);
        }}

        function stopAutoRefresh() {{
            if (refreshTimer) {{
                clearInterval(refreshTimer);
                refreshTimer = null;
            }}
        }}

        async function fetchLogs(promptId) {{
            try {{
                const resp = await fetch('/progress/' + promptId);
                const data = await resp.json();
                const logs = data.progress || [];
                if (logs.length) {{
                    document.getElementById("progressLogs").textContent = logs.join("\\n");
                }} else {{
                    document.getElementById("progressLogs").textContent = "(no logs found for this prompt_id)";
                }}
            }} catch (err) {{
                console.error("Error fetching logs:", err);
                document.getElementById("progressLogs").textContent = "(error)";
            }}
        }}

        document.addEventListener("DOMContentLoaded", () => {{
            setupAutoRefresh();

            const fetchLogsBtn = document.getElementById("fetchLogsBtn");
            fetchLogsBtn.addEventListener("click", () => {{
                const promptId = document.getElementById("promptIdInput").value.trim();
                if (promptId) {{
                    fetchLogs(promptId);
                }}
            }});
        }});
    </script>
    </body>
    </html>
    """
    return HTMLResponse(html_content)


# ==========================
# MAIN (CLI)
# ==========================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9000, help="Port to bind the server on.")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging output.")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging output (same as verbose).")
    args = parser.parse_args()

    # If --debug or --verbose is set, switch logger to DEBUG
    if args.debug or args.verbose:
        logger.setLevel(logging.DEBUG)

    uvicorn.run(app, host="0.0.0.0", port=args.port)
