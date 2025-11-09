# server.py
import os
import time
import threading
import signal
import urllib3
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Configuration (from environment)
URL = os.environ.get("TARGET_URL", "https://example.com").strip()
try:
    PORT = int(os.environ.get("PORT", "10000"))
except ValueError:
    PORT = 10000

RUN_LOOP = os.environ.get("RUN_LOOP", "true").strip().lower() != "false"
LOOP_PAUSE_SECS = float(os.environ.get("LOOP_PAUSE_SECS", "1"))

# BATCH_SIZE: how many requests each worker will send per batch
try:
    BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "43"))
    if BATCH_SIZE <= 0:
        raise ValueError("BATCH_SIZE must be positive")
except Exception:
    print("[WARN] Invalid BATCH_SIZE, falling back to 43", flush=True)
    BATCH_SIZE = 43

# WORKER_THREADS: how many concurrent worker threads to run
try:
    WORKER_THREADS = int(os.environ.get("WORKER_THREADS", "1"))
    if WORKER_THREADS < 1:
        raise ValueError("WORKER_THREADS must be >= 1")
except Exception:
    print("[WARN] Invalid WORKER_THREADS, falling back to 1", flush=True)
    WORKER_THREADS = 1

# Validate URL
_parsed = urlparse(URL)
if _parsed.scheme not in ("http", "https") or not _parsed.netloc:
    raise SystemExit(f"Bad TARGET_URL: {URL}")

# HTTP handler to keep service alive
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Service running - request test active")

stop_event = threading.Event()

def send_requests_once(session: requests.Session, url: str, worker_id: int):
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; RequestTest/1.0)",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "X-Forwarded-For": "127.0.0.1",
    }

    # Always show 43 in the start message per your request
    # include worker id to identify which thread logs belong to
    print(f"[INFO][worker-{worker_id}] Sending 43 requests to target: {url}", flush=True)

    # If BATCH_SIZE > 43 only print up to this many per batch
    max_print = 50 if BATCH_SIZE > 43 else BATCH_SIZE

    for i in range(BATCH_SIZE):
        if stop_event.is_set():
            print(f"[INFO][worker-{worker_id}] Stop requested; aborting batch.", flush=True)
            return
        try:
            r = session.get(url, headers=headers, timeout=8, verify=False)
            if i < max_print:
                print(f"[worker-{worker_id}]  [{i+1}/{BATCH_SIZE}] Status: {r.status_code}", flush=True)
        except Exception as e:
            if i < max_print:
                print(f"[worker-{worker_id}]  [{i+1}/{BATCH_SIZE}] Error: {type(e).__name__}: {e}", flush=True)
        # aggressive rate preserved
        time.sleep(0.18)

    print(f"[INFO][worker-{worker_id}] [DONE] Request sequence completed.", flush=True)

def worker_loop(worker_id: int):
    session = requests.Session()
    try:
        while not stop_event.is_set():
            send_requests_once(session, URL, worker_id)
            if not RUN_LOOP:
                break
            # pause between batches (respect fractional seconds)
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
        print(f"[INFO][worker-{worker_id}] Request worker exiting.", flush=True)

def handle_signal(signum, frame):
    print(f"[INFO] Signal {signum} received, shutting down...", flush=True)
    stop_event.set()

# Register signals for graceful shutdown
signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)

if __name__ == "__main__":
    # Start worker threads
    threads = []
    for wid in range(1, WORKER_THREADS + 1):
        t = threading.Thread(target=worker_loop, args=(wid,), daemon=True)
        t.start()
        threads.append(t)
        print(f"[INFO] Started worker thread {wid}", flush=True)

    # Start HTTP server to keep service alive
    httpd = HTTPServer(("", PORT), Handler)
    print(f"[SERVER] Web service live on port {PORT}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("[SERVER] Shutting down...", flush=True)
        stop_event.set()
        try:
            httpd.shutdown()
        except Exception:
            pass
        # give threads time to finish
        for t in threads:
            t.join(timeout=5)
        print("[SERVER] Exited.", flush=True)
