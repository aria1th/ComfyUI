import os
import uuid
import json
import logging
import asyncio
import httpx
import random
import ssl
import threading
import time
from collections import defaultdict
from typing import Optional
from fastapi import FastAPI, Request, Response, BackgroundTasks
from fastapi.responses import HTMLResponse, PlainTextResponse
import uvicorn
import websocket  # from "websocket-client" package

# ==========================
# CONFIGURATIONS & LOGGING
# ==========================

logger = logging.getLogger("Balancer")
logger.setLevel(logging.WARNING)
handler = logging.FileHandler("./balancer.log")
formatter = logging.Formatter(
    '%(asctime)s - %(levelname)s - %(clientip)s - "%(request_line)s" %(status_code)s %(message)s'
)
handler.setFormatter(formatter)
logger.addHandler(handler)

class ContextFilter(logging.Filter):
    def filter(self, record):
        record.clientip = getattr(record, "clientip", "unknown")
        return True

logger.addFilter(ContextFilter())

def default_logger_info(msg, extra=None):
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

worker_endpoints_str = os.getenv(
    "WORKER_ENDPOINTS",
    ""
)
WORKER_ENDPOINTS = [
    endpoint.strip() for endpoint in worker_endpoints_str.split(",") if endpoint.strip()
]

CONCURRENCY_LIMIT = 1
WORKER_SEMAPHORES = [asyncio.Semaphore(CONCURRENCY_LIMIT) for _ in WORKER_ENDPOINTS]

PROMPT_PROGRESS_LOGS = defaultdict(list)

# Track worker usage for each prompt_id
ACTIVE_CONNECTIONS = {}

# A global queue for requests that cannot find a slot right away
PENDING_QUEUE = asyncio.Queue()

# Lock to protect queue dispatch
enqueue_lock = asyncio.Lock()

# Protect access to PROMPT_PROGRESS_LOGS and ACTIVE_CONNECTIONS
progress_data_lock = threading.Lock()

# ==========================
# STATISTICS TRACKING
# ==========================

WORKER_STATS = {
    i: {
        "num_requests": 0,
        "total_time": 0.0,
        "max_time": 0.0,
        "min_time": float("inf"),
        "avg_time": 0.0,
    }
    for i in range(len(WORKER_ENDPOINTS))
}

GLOBAL_STATS = {
    "num_requests": 0,
    "total_time": 0.0,
    "max_time": 0.0,
    "min_time": float("inf"),
    "avg_time": 0.0,
}

stats_lock = asyncio.Lock()

def update_stats(worker_index: int, duration: float):
    wstats = WORKER_STATS[worker_index]
    wstats["num_requests"] += 1
    wstats["total_time"] += duration
    wstats["max_time"] = max(wstats["max_time"], duration)
    wstats["min_time"] = min(wstats["min_time"], duration)
    wstats["avg_time"] = wstats["total_time"] / wstats["num_requests"]

    GLOBAL_STATS["num_requests"] += 1
    GLOBAL_STATS["total_time"] += duration
    GLOBAL_STATS["max_time"] = max(GLOBAL_STATS["max_time"], duration)
    GLOBAL_STATS["min_time"] = min(GLOBAL_STATS["min_time"], duration)
    GLOBAL_STATS["avg_time"] = GLOBAL_STATS["total_time"] / GLOBAL_STATS["num_requests"]


# ==========================
# WEBSOCKET PROGRESS TRACKER
# ==========================

def track_worker_progress_ws_blocking(ws_url: str, client_id: str, prompt_id: str):
    """
    Blocks in a separate thread, reading progress from the worker's WebSocket
    and updating PROMPT_PROGRESS_LOGS / ACTIVE_CONNECTIONS.
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

                # If node_id is None and data['prompt_id'] == prompt_id => generation done
                if node_id is None and data.get("prompt_id") == prompt_id:
                    default_logger_info(f"[WS] {prompt_id} => Generation completed.")
                    with progress_data_lock:
                        PROMPT_PROGRESS_LOGS[prompt_id].append("Done")
                        if prompt_id in ACTIVE_CONNECTIONS:
                            ACTIVE_CONNECTIONS[prompt_id]["status"] = "done"
                    break

            elif msg_type == "status":
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
    t = threading.Thread(
        target=track_worker_progress_ws_blocking,
        args=(ws_url, client_id, prompt_id),
        daemon=True
    )
    t.start()


# ==========================
# Utility: "try_acquire_nowait" for asyncio.Semaphore
# ==========================

async def try_acquire_nowait(sem: asyncio.Semaphore) -> bool:
    """
    Attempt to acquire `sem` immediately (no wait).
    Return True if acquired successfully, False otherwise.
    """
    try:
        # If it can't be acquired immediately, we get a TimeoutError.
        await asyncio.wait_for(sem.acquire(), timeout=0.001)
        return True
    except asyncio.TimeoutError:
        return False


# ==========================
# Immediate Worker Acquisition
# ==========================

async def try_acquire_worker() -> Optional[int]:
    """
    Attempts an immediate concurrency slot on any worker, in random order.
    Returns the worker index if successful, or None if none is free.
    """
    indices = list(range(len(WORKER_ENDPOINTS)))
    random.shuffle(indices)
    for i in indices:
        got_it = await try_acquire_nowait(WORKER_SEMAPHORES[i])
        if got_it:
            return i
    return None

async def release_worker(worker_index: int):
    WORKER_SEMAPHORES[worker_index].release()


# ==========================
# Forward Request to Worker
# ==========================

async def handle_request_on_worker(
    worker_index: int,
    request_body: bytes,
    headers: dict,
    prompt_id_placeholder: str,
    request_obj: Request
) -> Response:
    start_time = time.monotonic()
    endpoint = WORKER_ENDPOINTS[worker_index]
    logger.info(
        f"Forwarding request to worker {worker_index}: {endpoint}",
        extra=get_extra_from_request(request_obj, 200),
    )

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                endpoint, content=request_body, headers=headers, timeout=TIMEOUT_SECONDS
            )
    except Exception as ex:
        logger.error(
            f"Error contacting worker {worker_index}: {ex}",
            extra=get_extra_from_request(request_obj, 500)
        )
        return Response(content="Error contacting worker", status_code=500)

    duration = time.monotonic() - start_time
    async with stats_lock:
        update_stats(worker_index, duration)

    # Parse out prompt_id if possible
    prompt_id = None
    try:
        resp_json = response.json()
        prompt_id = resp_json.get("prompt_id")
    except:
        logger.error(
            f"Error parsing worker response: {response.content}",
            extra=get_extra_from_request(request_obj, 500)
        )

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

        base_ws = endpoint.replace("http://", "ws://").replace("/prompt_sync", "")
        client_id = str(uuid.uuid4())
        ws_url = f"{base_ws}/ws?clientId={client_id}"
        launch_progress_thread(ws_url, client_id, prompt_id)

    else:
        # Fallback if we didn't get a real prompt_id
        prompt_id = prompt_id_placeholder
        logger.warning(
            f"No real prompt_id in response: {response.content}",
            extra=get_extra_from_request(request_obj, 200)
        )
        with progress_data_lock:
            ACTIVE_CONNECTIONS[prompt_id] = {
                "worker_index": worker_index,
                "status": "running (no real prompt_id)",
            }

    return Response(
        content=response.content,
        status_code=response.status_code,
        headers=dict(response.headers),
        media_type=response.headers.get("content-type"),
    )


# ==========================
# Dispatch Pending Queue
# ==========================

async def dispatch_pending_requests():
    """
    Drains PENDING_QUEUE if any worker is free. We attempt an immediate
    acquire of a worker slot. If successful, we assign that worker to the item
    and signal the item’s event, so the original request can proceed.
    """
    while True:
        try:
            request_item = PENDING_QUEUE.get_nowait()
        except asyncio.QueueEmpty:
            break

        async with enqueue_lock:
            worker_index = await try_acquire_worker()
            if worker_index is not None:
                request_item["worker_index"] = worker_index
                request_item["event"].set()
            else:
                # Put it back and stop if none was free
                await PENDING_QUEUE.put(request_item)
                break


# ==========================
# /prompt ENDPOINT
# ==========================

@app.post("/prompt")
async def prompt(request: Request, background_tasks: BackgroundTasks):
    """
    Receives a request. We attempt to acquire an immediate slot on any worker;
    if none is free, we queue the request until a worker frees up.
    """
    try:
        data = await request.body()
        if len(data) > MAX_REQUEST_SIZE:
            return Response(content="Request too large", status_code=413)

        headers = dict(request.headers)
        headers.pop("host", None)
        headers.pop("content-length", None)

        # 1. Try immediate concurrency
        worker_index = await try_acquire_worker()
        if worker_index is not None:
            # We got a slot right away
            try:
                resp = await handle_request_on_worker(
                    worker_index,
                    data,
                    headers,
                    f"temp_{uuid.uuid4()}",
                    request
                )
                return resp
            finally:
                await release_worker(worker_index)
                await dispatch_pending_requests()

        else:
            # 2. No immediate slot => queue it
            default_logger_info("All workers busy; queueing request.")
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
            await dispatch_pending_requests()

            # Wait for our item to get assigned a worker
            await item_event.wait()
            assigned_worker = queue_item["worker_index"]
            if assigned_worker is None:
                return Response(content="No worker assigned (unexpected)", status_code=500)

            try:
                resp = await handle_request_on_worker(
                    assigned_worker,
                    queue_item["request_body"],
                    queue_item["headers"],
                    queue_item["prompt_id_placeholder"],
                    queue_item["request_obj"],
                )
                return resp
            finally:
                await release_worker(assigned_worker)
                await dispatch_pending_requests()

    except Exception as e:
        err_message = f"Error in /prompt: {str(e)}"
        logger.error(err_message, extra=get_extra_from_request(request, 500))
        return Response(content="Internal Server Error", status_code=500)


# ==========================
# STATUS + LOGS
# ==========================

@app.get("/status")
async def status():
    status_info = []
    for i, endpoint in enumerate(WORKER_ENDPOINTS):
        concurrency_in_use = CONCURRENCY_LIMIT - WORKER_SEMAPHORES[i]._value
        requests_waiting = len(WORKER_SEMAPHORES[i]._waiters) if WORKER_SEMAPHORES[i]._waiters else 0
        worker_status = "idle" if concurrency_in_use == 0 else "processing"

        min_time = (
            WORKER_STATS[i]["min_time"] if WORKER_STATS[i]["min_time"] != float("inf") else 0.0
        )
        status_info.append({
            "worker_index": i,
            "endpoint": endpoint,
            "status": worker_status,
            "concurrency_in_use": concurrency_in_use,
            "requests_waiting": requests_waiting,
            "stats": {
                "num_requests": WORKER_STATS[i]["num_requests"],
                "avg_time": round(WORKER_STATS[i]["avg_time"], 4),
                "max_time": round(WORKER_STATS[i]["max_time"], 4),
                "min_time": round(min_time, 4),
            }
        })

    global_min = GLOBAL_STATS["min_time"] if GLOBAL_STATS["min_time"] != float("inf") else 0.0
    global_stats = {
        "num_requests": GLOBAL_STATS["num_requests"],
        "avg_time": round(GLOBAL_STATS["avg_time"], 4),
        "max_time": round(GLOBAL_STATS["max_time"], 4),
        "min_time": round(global_min, 4),
    }

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
        "global_stats": global_stats,
        "active_jobs": active_jobs_list,
        "pending_queue_length": PENDING_QUEUE.qsize(),
        "known_prompts_progress": list(PROMPT_PROGRESS_LOGS.keys()),
    }

@app.get("/progress/{prompt_id}")
async def get_progress(prompt_id: str):
    with progress_data_lock:
        logs = PROMPT_PROGRESS_LOGS.get(prompt_id, [])
        return {"prompt_id": prompt_id, "progress": logs}

@app.get("/logs", response_class=PlainTextResponse)
async def get_logs():
    log_file_path = "./balancer.log"
    if not os.path.exists(log_file_path):
        return "No log file found."

    NUM_LINES = 200
    try:
        with open(log_file_path, "rb") as f:
            f.seek(0, 2)
            file_size = f.tell()
            block_size = 1024
            data = b""
            lines_found = 0
            cursor = file_size

            while cursor > 0 and lines_found < NUM_LINES:
                cursor = max(0, cursor - block_size)
                f.seek(cursor)
                chunk = f.read(block_size)
                data = chunk + data
                lines_found = data.count(b"\n")

            all_lines = data.splitlines()
            if len(all_lines) > NUM_LINES:
                all_lines = all_lines[-NUM_LINES:]
            return "\n".join(line.decode("utf-8", errors="replace") for line in all_lines)
    except Exception as ex:
        return f"Error reading logs: {ex}"

@app.get("/status_page", response_class=HTMLResponse)
async def status_page():
    """
    Simple HTML page that periodically fetches /status for a quick dashboard.
    """
    global CONCURRENCY_LIMIT
    html_content = f"""\
    <!DOCTYPE html>
    <html>
    <head>
        <title>Worker Status</title>
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
                    Auto-Refresh (every 5s)
                  </label>
                </div>
            </div>
        </div>

        <h4>Global Stats</h4>
        <ul>
            <li><strong>#Requests:</strong> <span id="globalNumRequests">0</span></li>
            <li><strong>Avg Time (s):</strong> <span id="globalAvgTime">0</span></li>
            <li><strong>Max Time (s):</strong> <span id="globalMaxTime">0</span></li>
            <li><strong>Min Time (s):</strong> <span id="globalMinTime">0</span></li>
        </ul>

        <table class="table table-bordered table-hover align-middle" id="statusTable">
            <thead class="table-secondary">
                <tr>
                    <th>Worker Index</th>
                    <th>Endpoint</th>
                    <th>Status</th>
                    <th>Concurrency Usage</th>
                    <th>Requests Waiting</th>
                    <th>Stats (#Req / Avg / Max / Min)</th>
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

        <hr class="my-4">

        <h3>Production Logs</h3>
        <button class="btn btn-secondary mb-2" id="fetchProdLogsBtn">Fetch Logs Tail (200 lines)</button>
        <div id="prodLogs" class="log-area">(no logs)</div>
    </div>

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    <script>
        const refreshIntervalMs = 5000;
        let autoRefreshEnabled = true;
        let refreshTimer = null;

        async function fetchStatus() {{
            try {{
                const response = await fetch('/status');
                const data = await response.json();

                document.getElementById("globalNumRequests").textContent = data.global_stats.num_requests;
                document.getElementById("globalAvgTime").textContent = data.global_stats.avg_time;
                document.getElementById("globalMaxTime").textContent = data.global_stats.max_time;
                document.getElementById("globalMinTime").textContent = data.global_stats.min_time;

                const tbody = document.querySelector("#statusTable tbody");
                tbody.innerHTML = "";

                let totalUsage = 0;
                const concurrencyLimit = {CONCURRENCY_LIMIT};

                data.workers.forEach((item) => {{
                    const row = document.createElement("tr");

                    const cellIndex = document.createElement("td");
                    cellIndex.textContent = item.worker_index;

                    const cellEndpoint = document.createElement("td");
                    cellEndpoint.textContent = item.endpoint;

                    const cellStatus = document.createElement("td");
                    cellStatus.textContent = item.status;
                    cellStatus.classList.add(
                        item.status === "idle" ? "status-idle" : "status-processing"
                    );

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

                    const cellWaiting = document.createElement("td");
                    cellWaiting.textContent = item.requests_waiting;

                    const cellStats = document.createElement("td");
                    const s = item.stats;
                    cellStats.textContent = `${{s.num_requests}} / ${{s.avg_time}}s / ${{s.max_time}}s / ${{s.min_time}}s`;

                    row.appendChild(cellIndex);
                    row.appendChild(cellEndpoint);
                    row.appendChild(cellStatus);
                    row.appendChild(cellConcurrency);
                    row.appendChild(cellWaiting);
                    row.appendChild(cellStats);

                    tbody.appendChild(row);

                    totalUsage += item.concurrency_in_use;
                }});

                document.getElementById("totalUsage").textContent = totalUsage;
                document.getElementById("globalQueue").textContent = data.pending_queue_length || 0;

                const now = new Date();
                document.getElementById("lastUpdated").textContent = "Last updated: " + now.toLocaleTimeString();

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
                if (!activeJobsHtml) {{
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

        async function fetchProductionLogs() {{
            try {{
                const resp = await fetch('/logs');
                const textData = await resp.text();
                document.getElementById("prodLogs").textContent = textData;
            }} catch (err) {{
                console.error("Error fetching production logs:", err);
                document.getElementById("prodLogs").textContent = "(error)";
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

            const fetchProdLogsBtn = document.getElementById("fetchProdLogsBtn");
            fetchProdLogsBtn.addEventListener("click", () => {{
                fetchProductionLogs();
            }});
        }});
    </script>
    </body>
    </html>
    """
    return HTMLResponse(html_content)

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9000, help="Port to bind the server on.")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging output.")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging output (same as verbose).")
    args = parser.parse_args()

    if args.debug or args.verbose:
        logger.setLevel(logging.DEBUG)

    uvicorn.run(app, host="0.0.0.0", port=args.port)
