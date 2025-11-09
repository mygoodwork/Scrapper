# server.py
import requests
import time
import urllib3
from http.server import HTTPServer, BaseHTTPRequestHandler

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Target URL
url = "https://c2c.binance.com/en/fiatOrderDetail?orderNo=22811436844419928064&createdAt=1760455623723"

# Simple HTTP handler to keep Render service alive
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Service running - request test active")

def send_requests():
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; RequestTest/1.0)",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "X-Forwarded-For": "127.0.0.1"
    }
    
    print(f"[INFO] Sending 43 requests to target...")
    for i in range(43):
        try:
            r = requests.get(url, headers=headers, timeout=8, verify=False)
            print(f"  [{i+1}/43] Status: {r.status_code}")
            time.sleep(0.18)
        except Exception as e:
            print(f"  [{i+1}/43] Error: {type(e).__name__}")
    
    print("[DONE] Request sequence completed.")

# Execute once at startup
send_requests()

# Keep the web service alive on Render
print("[SERVER] Web service now live on port 10000")
httpd = HTTPServer(('', 10000), Handler)
httpd.serve_forever()
