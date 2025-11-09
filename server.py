# server.py
"""
Safe combined penetration-test runner.

REQUIREMENT: set I_OWN_TARGET=true in environment before enabling traffic.
This script is intended to be used ONLY against systems you own or have written permission to test.
"""
import os
import time
import threading
import signal
import random
import urllib3
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# -------------------------
# Environment configuration
# -------------------------
I_OWN_TARGET = os.environ.get("I_OWN_TARGET", "false").strip().lower() == "true"

ENDPOINT_URL = os.environ.get("ENDPOINT_URL", "https://example.com/api/test").strip()
ORDER_NO = os.environ.get("ORDER_NO", "22811436844419928064")
BNC = os.environ.get("BNC", "")
METHOD = os.environ.get("METHOD", "GET").strip().upper()

try:
    WORKER_THREADS = int(os.environ.get("WORKER_THREADS", "2"))
except Exception:
    WORKER_THREADS = 2
try:
    BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "43"))
except Exception:
    BATCH_SIZE = 43
try:
    REQUEST_DELAY_MS = float(os.environ.get("REQUEST_DELAY_MS", "180"))
except Exception:
    REQUEST_DELAY_MS = 180.0

RUN_LOOP = os.environ.get("RUN_LOOP", "true").strip().lower() != "false"

try:
    LOOP_PAUSE_SECS = float(os.environ.get("LOOP_PAUSE_SECS", "1"))
except Exception:
    LOOP_PAUSE_SECS = 1.0

MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "200"))
MAX_BATCH_SIZE = int(os.environ.get("MAX_BATCH_SIZE", "1000000"))

VERIFY_SSL = os.environ.get("VERIFY_SSL", "false").strip().lower() == "true"
PRINT_LIMIT = int(os.environ.get("PRINT_LIMIT", "50"))
REQUEST_TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT", "8"))

COOKIE_NAME = os.environ.get("COOKIE_NAME", "BNC")
REFERER = os.environ.get("REFERER", "https://p2p.binance.com/en/trade/orderDetail?orderNo={ORDER_NO}")
ORIGIN = os.environ.get("ORIGIN", REFERER)

SUMMARY_INTERVAL = float(os.environ.get("SUMMARY_INTERVAL", "10"))

# enforce safety caps
WORKER_THREADS = max(1, min(WORKER_THREADS, MAX_WORKERS))
if BATCH_SIZE < 1:
    BATCH_SIZE = 1
if BATCH_SIZE > MAX_BATCH_SIZE:
    print(f"[WARN] BATCH_SIZE capped from {BATCH_SIZE} to {MAX_BATCH_SIZE}", flush=True)
    BATCH_SIZE = MAX_BATCH_SIZE

_parsed = urlparse(ENDPOINT_URL)
if _parsed.scheme not in ("http", "https") or not _parsed.netloc:
    raise SystemExit(f"Bad ENDPOINT_URL: {ENDPOINT_URL}")

# -------------------------
# HTTP keep-alive server
# -------------------------
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"PenTest runner alive")

# -------------------------
# Shared objects
# -------------------------
stop_event = threading.Event()
counters_lock = threading.Lock()
counters = {
    "sent": 0,
    "success": 0,
    "status_counts": {},
    "errors": 0
}

# -------------------------
# Helper functions
# -------------------------
def inc_counter(key, subkey=None):
    with counters_lock:
        if subkey is None:
            counters[key] = counters.get(key, 0) + 1
        else:
            if key not in counters:
                counters[key] = {}
            counters[key][subkey] = counters[key].get(subkey, 0) + 1

def build_headers(worker_id: int):
    return {
        "User-Agent": f"Mozilla/5.0 (Linux; Android 13; Pixel 7) PentestWorker/{worker_id}",
        "Accept": "application/json",
        "Referer": REFERER,
        "Origin": ORIGIN
    }

def build_cookies():
    return {COOKIE_NAME: BNC} if BNC else {}

def build_params():
    return {
        "orderNo": ORDER_NO,
        "page": random.randint(1, 3),
        "rows": 50,
        "timestamp": int(time.time() * 1000),
    }

def handle_rate_limit(response):
    ra = response.headers.get("Retry-After")
    if ra:
        try:
            return float(ra)
        except Exception:
            try:
                return int(ra)
            except Exception:
                return None
    return None

def log_error_details(worker_id: int, i: int, r=None, params=None, exc=None):
    print("="*80, flush=True)
    print(f"[DEBUG][worker-{worker_id}] Request #{i+1} detailed log:", flush=True)
    if exc:
        print(f"  Exception: {type(exc).__name__}: {exc}", flush=True)
    if r is not None:
        try:
            print(f"  Status: {r.status_code}", flush=True)
            try:
                print(f"  Response headers: {dict(r.headers)}", flush=True)
            except Exception:
                pass
            try:
                content = r.text
                if len(content) > 2000:
                    content = content[:2000] + " ...[truncated]"
                print(f"  Response body (up to 2000 chars):\n{content}", flush=True)
            except Exception as e:
                print(f"  Failed to read response body: {e}", flush=True)
        except Exception as e:
            print(f"  Error while inspecting response object: {e}", flush=True)
    if params:
        print(f"  Params/Body: {params}", flush=True)
    cookies = build_cookies()
    if cookies:
        print(f"  Cookies: {cookies}", flush=True)
    headers = build_headers(worker_id)
    print(f"  Headers: {headers}", flush=True)
    print("="*80, flush=True)

# -------------------------
# Worker logic
# -------------------------
def send_requests_once(session: requests.Session, worker_id: int):
    headers = build_headers(worker_id)
    cookies = build_cookies()
    max_print = PRINT_LIMIT if BATCH_SIZE > 43 else BATCH_SIZE
    print(f"[INFO][worker-{worker_id}] Sending 43 requests to: {ENDPOINT_URL}", flush=True)

    for i in range(BATCH_SIZE):
        if stop_event.is_set():
            print(f"[INFO][worker-{worker_id}] Stop requested; aborting batch.", flush=True)
            return

        params = None
        r = None
        try:
            params = build_params()
            if METHOD == "POST":
                r = session.post(
                    ENDPOINT_URL,
                    headers=headers,
                    cookies=cookies,
                    json=params,
                    timeout=REQUEST_TIMEOUT,
                    verify=VERIFY_SSL,
                )
            else:
                r = session.get(
                    ENDPOINT_URL,
                    headers=headers,
                    cookies=cookies,
                    params=params,
                    timeout=REQUEST_TIMEOUT,
                    verify=VERIFY_SSL,
                )

            inc_counter("sent")
            with counters_lock:
                counters["status_counts"][r.status_code] = counters["status_counts"].get(r.status_code, 0) + 1
                if 200 <= r.status_code < 300:
                    counters["success"] = counters.get("success", 0) + 1

            if i < max_print:
                print(f"[worker-{worker_id}] [{i+1}/{BATCH_SIZE}] → {r.status_code}", flush=True)

            if r.status_code >= 400:
                inc_counter("errors")
                log_error_details(worker_id, i, r=r, params=params)

            if r.status_code == 429:
                wait = handle_rate_limit(r)
                if wait is None:
                    wait = min(60, (2 ** min(i, 6)) * 0.5)
                print(f"[worker-{worker_id}] 429 received, backing off {wait}s", flush=True)
                t0 = time.time()
                while (time.time() - t0) < wait:
                    if stop_event.is_set():
                        return
                    time.sleep(0.5)

        except requests.RequestException as e:
            inc_counter("errors")
            log_error_details(worker_id, i, r=r, params=params, exc=e)
            if i < max_print:
                print(f"[worker-{worker_id}] [{i+1}/{BATCH_SIZE}] RequestException: {type(e).__name__}: {e}", flush=True)
        except Exception as e:
            inc_counter("errors")
            log_error_details(worker_id, i, r=r, params=params, exc=e)
            if i < max_print:
                print(f"[worker-{worker_id}] [{i+1}/{BATCH_SIZE}] Unexpected: {type(e).__name__}: {e}", flush=True)

        # pacing
        delay_s = REQUEST_DELAY_MS / 1000.0
        slept = 0.0
        while slept < delay_s:
            if stop_event.is_set():
                return
            step = min(0.2, delay_s - slept)
            time.sleep(step)
            slept += step

def worker_loop(worker_id: int):
    session = requests.Session()
    session.headers.update({"Accept": "application/json"})
    try:
        session.cookies.update(build_cookies())
        while not stop_event.is_set():
            if not I_OWN_TARGET:
                if worker_id == 1:
                    print("[WARN] I_OWN_TARGET not true — traffic sender disabled. Set env I_OWN_TARGET=true to enable.", flush=True)
                time.sleep(5)
                continue

            send_requests_once(session, worker_id)
            if not RUN_LOOP:
                break

            # pause between batches
            whole = int(LOOP_PAUSE_SECS)
            for _ in range(whole):
                if stop_event.is_set():
                    break
                time.sleep(1)
            frac = LOOP_PAUSE_SECS - whole
            if frac and not stop_event.is_set():
                time.sleep(frac)
    finally:
        session.close()
        print(f"[INFO][worker-{worker_id}] Exiting.", flush=True)

# -------------------------
# Summary reporter
# -------------------------
def summary_reporter():
    last_sent = 0
    while not stop_event.is_set():
        time.sleep(SUMMARY_INTERVAL)
        with counters_lock:
            sent = counters.get("sent", 0)
            success = counters.get("success", 0)
            errors = counters.get("errors", 0)
            status_counts = counters.get("status_counts", {}).copy()
        delta = sent - last_sent
        last_sent = sent
        print(f"[SUMMARY] sent_total={sent} sent_delta={delta} success={success} errors={errors} status_counts={status_counts}", flush=True)

# -------------------------
# Signal handling & main
# -------------------------
def handle_signal(signum, frame):
    print(f"[INFO] Signal {signum} received → shutting down...", flush=True)
    stop_event.set()

signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)

if __name__ == "__main__":
    print("[START] PenTest runner starting...", flush=True)
    threads = []

    t_sum = threading.Thread(target=summary_reporter, daemon=True)
    t_sum.start()

    for wid in range(1, WORKER_THREADS + 1):
        t = threading.Thread(target=worker_loop, args=(wid,), daemon=True)
        t.start()
        threads.append(t)
        print(f"[INFO] Started worker {wid}", flush=True)

    try:
        port = int(os.environ.get("PORT", "10000"))
    except Exception:
        port = 10000
    httpd = HTTPServer(("", port), Handler)
    print(f"[SERVER] Keep-alive server listening on port {port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        print("[SHUTDOWN] Waiting for workers to finish...", flush=True)
        for t in threads:
            t.join(timeout=5)
        t_sum.join(timeout=1)
        try:
            httpd.shutdown()
        except Exception:
            pass
        print("[EXIT] PenTest runner exited.", flush=True)
