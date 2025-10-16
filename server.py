#!/usr/bin/env python3
"""
login_auto_discover_v2.py

Enhanced automatic login simulator:
- Fetches frontend HTML + JS to auto-discover hidden API endpoints.
- Simulates a browser login like PayPal (no Selenium needed).
"""

import os
import re
import sys
import json
import requests
from typing import Optional
from urllib.parse import urljoin

# --- Config ---
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://smarts9trading.netlify.app/login")
EMAIL = os.getenv("LOGIN_EMAIL", "")
PASSWORD = os.getenv("LOGIN_PASSWORD", "")

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

REQUEST_TIMEOUT = 15

# Common patterns that appear in JS bundles
LOGIN_PATTERNS = [
    r'["\'](/api/login[^"\']*)["\']',
    r'["\'](/auth/login[^"\']*)["\']',
    r'["\'](/v1/oauth2/login[^"\']*)["\']',
    r'["\'](/user/login[^"\']*)["\']',
    r'["\'](/signin[^"\']*)["\']',
    r'["\'](/api/v\d+/auth/login[^"\']*)["\']',
]


def discover_login_endpoint(html: str, base_url: str, session: requests.Session) -> Optional[str]:
    """Search HTML and JS files for likely login endpoint."""
    # 1️⃣ Directly check HTML first
    for pattern in LOGIN_PATTERNS:
        m = re.search(pattern, html, re.I)
        if m:
            endpoint = urljoin(base_url, m.group(1))
            print(f"🔍 Found login endpoint in HTML: {endpoint}")
            return endpoint

    # 2️⃣ Check linked JS bundles
    js_links = re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', html)
    found_endpoints = []

    for js_path in js_links:
        if not js_path.endswith((".js", ".mjs")):
            continue
        js_url = urljoin(base_url, js_path)
        print(f"🧩 Fetching JS bundle: {js_url}")
        try:
            js_resp = session.get(js_url, timeout=10)
            for pattern in LOGIN_PATTERNS:
                matches = re.findall(pattern, js_resp.text, re.I)
                for m in matches:
                    found = urljoin(base_url, m)
                    if found not in found_endpoints:
                        found_endpoints.append(found)
                        print(f"✅ Found possible login endpoint in JS: {found}")
        except requests.RequestException as e:
            print(f"⚠️ Skipped {js_url} ({e})")

    # Return the most specific endpoint (longest path usually)
    if found_endpoints:
        found_endpoints.sort(key=len, reverse=True)
        return found_endpoints[0]

    print("⚠️ No login endpoint found automatically.")
    return None


def extract_csrf_from_html(html: str) -> Optional[dict]:
    """Extract CSRF tokens if present."""
    patterns = [
        r'<meta[^>]*name=["\']csrf-token["\'][^>]*content=["\']([^"\']+)["\']',
        r'<input[^>]*type=["\']hidden["\'][^>]*name=["\']csrf_token["\'][^>]*value=["\']([^"\']+)["\']',
        r'window\.(?:CSRF_TOKEN|csrfToken|__CSRF__)\s*=\s*["\']([^"\']+)["\']',
    ]
    for p in patterns:
        m = re.search(p, html, re.I)
        if m:
            return {"name": "csrf-token", "value": m.group(1)}
    return None


def attempt_login(session: requests.Session, login_url: str, csrf: Optional[dict]):
    """Try login with and without CSRF tokens."""
    payload = {"email": EMAIL, "password": PASSWORD}
    headers = {
        "User-Agent": DEFAULT_HEADERS["User-Agent"],
        "Accept": "application/json, text/plain, */*",
        "Origin": FRONTEND_URL.rsplit("/", 1)[0],
        "Referer": FRONTEND_URL,
        "X-Requested-With": "XMLHttpRequest",
    }
    if csrf:
        headers["X-CSRF-Token"] = csrf["value"]

    print(f"\n🚀 Attempting login at: {login_url}")
    try:
        resp = session.post(login_url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT)
        print(f"➡️  POST status: {resp.status_code}")
        return resp
    except requests.RequestException as e:
        print("🚫 Request failed:", e)
        return None


def main():
    if not EMAIL or not PASSWORD:
        print("❗ Please set LOGIN_EMAIL and LOGIN_PASSWORD environment variables.")
        sys.exit(1)

    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)

    print(f"🌐 Loading frontend page: {FRONTEND_URL}")
    resp = session.get(FRONTEND_URL, timeout=REQUEST_TIMEOUT)
    html = resp.text

    csrf = extract_csrf_from_html(html)
    if csrf:
        print(f"🛡️ Found CSRF token: {csrf}")
    else:
        print("ℹ️ No CSRF token found.")

    login_url = discover_login_endpoint(html, FRONTEND_URL, session)
    if not login_url:
        print("❌ Could not detect login endpoint automatically.")
        return

    final = attempt_login(session, login_url, csrf)
    if not final:
        print("❌ Login request failed.")
        return

    print("\n📦 Response:")
    try:
        j = final.json()
        print(json.dumps(j, indent=2))
        if "access_token" in j or "token" in j:
            print("✅ Logged in successfully — token detected!")
        else:
            print("⚠️ Login succeeded, but no token field found.")
    except Exception:
        print(final.text[:800])


if __name__ == "__main__":
    main()
