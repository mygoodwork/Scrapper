# server.py
import os
import time
import threading
import signal
import urllib3
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# =====================================================
# CONFIGURATION
# =====================================================

# Direct endpoint for testing (e.g. https://dansog-backend.onrender.com/api/dashboard/stats)
ENDPOINT_URL = os.environ.get("ENDPOINT_URL", "https://example.com/api/test").strip()

# Port for local HTTP server (to keep process alive)
PORT = int(os.environ.get("PORT", "10000"))

# Looping controls
RUN_LOOP = os.environ.get("RUN_LOOP", "true").strip().lower() != "false"
LOOP_PAUSE_SECS = float(os.environ.get("LOOP_PAUSE_SECS", "1"))

# Requests per worker
try:
    BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "43"))
    if BATCH_SIZE <= 0:
        raise ValueError
except Exception:
    print("[WARN] Invalid BATCH_SIZE, using 43", flush=True)
    BATCH_SIZE = 43

# Number of threads
try:
    WORKER_THREADS = int(os.environ.get("WORKER_THREADS", "2"))
    if WORKER_THREADS < 1:
        raise ValueError
except Exception:
    print("[WARN] Invalid WORKER_THREADS, using 1", flush=True)
    WORKER_THREADS = 1

# =====================================================
# SERVER + SIGNAL SETUP
# =====================================================
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Pentest active - single endpoint mode")

stop_event = threading.Event()

# =====================================================
# REQUEST LOGIC
# =====================================================
def send_requests_once(session: requests.Session, endpoint_url: str, worker_id: int):
    headers = {
        "User-Agent": f"Mozilla/5.0 (compatible; PentestWorker/{worker_id})",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    print(f"[INFO][worker-{worker_id}] Sending 43 requests to: {endpoint_url}", flush=True)
    max_print = 50 if BATCH_SIZE > 43 else BATCH_SIZE

    for i in range(BATCH_SIZE):
        if stop_event.is_set():
            print(f"[INFO][worker-{worker_id}] Stop requested; aborting batch.", flush=True)
            return

        try:
            r = session.get(endpoint_url, headers=headers, timeout=8, verify=False)
            if i < max_print:
                print(f"[worker-{worker_id}] [{i+1}/{BATCH_SIZE}] → Status: {r.status_code}", flush=True)
        except Exception as e:
            if i < max_print:
                print(f"[worker-{worker_id}] [{i+1}/{BATCH_SIZE}] → Error: {type(e).__name__}: {e}", flush=True)

        # maintain realistic spacing between hits
        time.sleep(0.18)

    print(f"[INFO][worker-{worker_id}] Batch completed.", flush=True)


def worker_loop(worker_id: int):
    session = requests.Session()
    try:
        while not stop_event.is_set():
            send_requests_once(session, ENDPOINT_URL, worker_id)
            if not RUN_LOOP:
                break
            time.sleep(LOOP_PAUSE_SECS)
    finally:
        session.close()
        print(f"[INFO][worker-{worker_id}] Exiting.", flush=True)


# =====================================================
# SIGNAL HANDLERS
# =====================================================
def handle_signal(signum, frame):
    print(f"[INFO] Signal {signum} received → stopping workers...", flush=True)
    stop_event.set()

signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)

# =====================================================
# MAIN ENTRY
# =====================================================
if __name__ == "__main__":
    threads = []
    for wid in range(1, WORKER_THREADS + 1):
        t = threading.Thread(target=worker_loop, args=(wid,), daemon=True)
        t.start()
        threads.append(t)
        print(f"[INFO] Worker {wid} started.", flush=True)

    httpd = HTTPServer(("", PORT), Handler)
    print(f"[SERVER] Pentest service live on port {PORT}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("[SERVER] Shutdown initiated...", flush=True)
        stop_event.set()
        try:
            httpd.shutdown()
        except Exception:
            pass
        for t in threads:
            t.join(timeout=5)
        print("[SERVER] Exited cleanly.", flush=True)
