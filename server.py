# server.py
import os
import time
import threading
import urllib3
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Configuration
URL = os.environ.get("TARGET_URL", "https://example.com").strip()
PORT = int(os.environ.get("PORT", "10000"))
RUN_LOOP = os.environ.get("RUN_LOOP", "true").strip().lower() != "false"
LOOP_PAUSE_SECS = float(os.environ.get("LOOP_PAUSE_SECS", "1"))
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "43"))

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

def send_requests_once(session: requests.Session, url: str):
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; RequestTest/1.0)",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "X-Forwarded-For": "127.0.0.1",
    }

    # Always print start as 43
    print(f"[INFO] Sending 43 requests to target: {url}", flush=True)

    # Determine max logs to print
    max_print = 50 if BATCH_SIZE > 43 else BATCH_SIZE

    for i in range(BATCH_SIZE):
        if stop_event.is_set():
            print("[INFO] Stop requested; aborting batch.", flush=True)
            return
        try:
            r = session.get(url, headers=headers, timeout=8, verify=False)
            # Only print first max_print requests
            if i < max_print:
                print(f"  [{i+1}/{BATCH_SIZE}] Status: {r.status_code}", flush=True)
        except Exception as e:
            if i < max_print:
                print(f"  [{i+1}/{BATCH_SIZE}] Error: {type(e).__name__}: {e}", flush=True)
        # aggressive rate preserved
        time.sleep(0.18)

    print("[DONE] Request sequence completed.", flush=True)

def send_requests_worker():
    session = requests.Session()
    try:
        while not stop_event.is_set():
            send_requests_once(session, URL)
            if not RUN_LOOP:
                break
            # pause between batches
            for _ in range(int(LOOP_PAUSE_SECS)):
                if stop_event.is_set():
                    break
                time.sleep(1)
            frac = LOOP_PAUSE_SECS - int(LOOP_PAUSE_SECS)
            if frac and not stop_event.is_set():
                time.sleep(frac)
    finally:
        session.close()
        print("[INFO] Request worker exiting.", flush=True)

if __name__ == "__main__":
    # Start aggressive sender in background
    t = threading.Thread(target=send_requests_worker, daemon=True)
    t.start()

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
        t.join(timeout=2)
        print("[SERVER] Exited.", flush=True)
