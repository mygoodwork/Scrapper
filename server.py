import os
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests
import logging
from typing import Dict, Optional
from dataclasses import dataclass
from contextlib import contextmanager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(levelname)s][%(asctime)s][%(threadName)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Configuration constants
REQUEST_TIMEOUT = 8
REQUEST_DELAY = 0.18
MAX_PRINT_LINES = 50
THREAD_JOIN_TIMEOUT = 5
DEFAULT_BATCH_SIZE = 43
DEFAULT_WORKER_THREADS = 4
DEFAULT_LOOP_PAUSE = 1.0
MAX_WORKER_THREADS = 100
MAX_BATCH_SIZE = 1000

# Global metrics
@dataclass
class Metrics:
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    total_response_time: float = 0.0
    errors_by_type: Dict[str, int] = None
    
    def __post_init__(self):
        if self.errors_by_type is None:
            self.errors_by_type = {}
    
    def record_success(self, response_time: float):
        self.total_requests += 1
        self.successful_requests += 1
        self.total_response_time += response_time
    
    def record_failure(self, error_type: str):
        self.total_requests += 1
        self.failed_requests += 1
        self.errors_by_type[error_type] = self.errors_by_type.get(error_type, 0) + 1
    
    def get_stats(self) -> dict:
        avg_response_time = (
            self.total_response_time / self.successful_requests 
            if self.successful_requests > 0 else 0
        )
        success_rate = (
            (self.successful_requests / self.total_requests * 100) 
            if self.total_requests > 0 else 0
        )
        return {
            'total_requests': self.total_requests,
            'successful': self.successful_requests,
            'failed': self.failed_requests,
            'success_rate': f"{success_rate:.2f}%",
            'avg_response_time': f"{avg_response_time:.3f}s",
            'errors': self.errors_by_type
        }

metrics = Metrics()
metrics_lock = threading.Lock()

def validate_config():
    """Validate environment configuration"""
    global BATCH_SIZE, WORKER_THREADS, LOOP_PAUSE_SECS
    
    # Validate BATCH_SIZE
    try:
        BATCH_SIZE = int(os.environ.get("BATCH_SIZE", str(DEFAULT_BATCH_SIZE)))
        if BATCH_SIZE <= 0:
            raise ValueError("BATCH_SIZE must be positive")
        if BATCH_SIZE > MAX_BATCH_SIZE:
            logger.warning(f"BATCH_SIZE {BATCH_SIZE} exceeds maximum {MAX_BATCH_SIZE}, capping")
            BATCH_SIZE = MAX_BATCH_SIZE
    except (ValueError, TypeError) as e:
        logger.error(f"Invalid BATCH_SIZE: {e}, falling back to {DEFAULT_BATCH_SIZE}")
        BATCH_SIZE = DEFAULT_BATCH_SIZE
    
    # Validate WORKER_THREADS
    try:
        WORKER_THREADS = int(os.environ.get("WORKER_THREADS", str(DEFAULT_WORKER_THREADS)))
        if WORKER_THREADS <= 0:
            raise ValueError("WORKER_THREADS must be positive")
        if WORKER_THREADS > MAX_WORKER_THREADS:
            logger.warning(f"WORKER_THREADS {WORKER_THREADS} exceeds maximum {MAX_WORKER_THREADS}, capping")
            WORKER_THREADS = MAX_WORKER_THREADS
    except (ValueError, TypeError) as e:
        logger.error(f"Invalid WORKER_THREADS: {e}, falling back to {DEFAULT_WORKER_THREADS}")
        WORKER_THREADS = DEFAULT_WORKER_THREADS
    
    # Validate LOOP_PAUSE_SECS
    try:
        LOOP_PAUSE_SECS = float(os.environ.get("LOOP_PAUSE_SECS", str(DEFAULT_LOOP_PAUSE)))
        if LOOP_PAUSE_SECS < 0:
            raise ValueError("LOOP_PAUSE_SECS cannot be negative")
    except (ValueError, TypeError) as e:
        logger.error(f"Invalid LOOP_PAUSE_SECS: {e}, falling back to {DEFAULT_LOOP_PAUSE}")
        LOOP_PAUSE_SECS = DEFAULT_LOOP_PAUSE
    
    logger.info(f"Configuration: BATCH_SIZE={BATCH_SIZE}, WORKER_THREADS={WORKER_THREADS}, LOOP_PAUSE_SECS={LOOP_PAUSE_SECS}")

# Initialize configuration
validate_config()

@contextmanager
def create_session():
    """Create a properly configured requests session with connection pooling"""
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=10,
        pool_maxsize=20,
        max_retries=requests.adapters.Retry(
            total=3,
            backoff_factor=0.3,
            status_forcelist=[500, 502, 503, 504]
        )
    )
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    
    try:
        yield session
    finally:
        session.close()

def make_request(session: requests.Session, url: str, headers: Optional[Dict[str, str]] = None) -> tuple[bool, float, Optional[str]]:
    """
    Make a single HTTP request and return success status, response time, and error if any
    
    Returns:
        tuple: (success: bool, response_time: float, error_message: Optional[str])
    """
    start_time = time.time()
    try:
        response = session.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        response_time = time.time() - start_time
        
        # Log response preview
        try:
            content = response.text[:MAX_PRINT_LINES] if len(response.text) > MAX_PRINT_LINES else response.text
            logger.debug(f"Response ({response.status_code}): {content}")
        except Exception as e:
            logger.debug(f"Could not read response content: {e}")
        
        return True, response_time, None
        
    except requests.exceptions.Timeout:
        response_time = time.time() - start_time
        return False, response_time, "Timeout"
    except requests.exceptions.ConnectionError as e:
        response_time = time.time() - start_time
        return False, response_time, "ConnectionError"
    except requests.exceptions.RequestException as e:
        response_time = time.time() - start_time
        return False, response_time, f"RequestException: {type(e).__name__}"
    except Exception as e:
        response_time = time.time() - start_time
        logger.error(f"Unexpected error: {e}")
        return False, response_time, f"UnexpectedException: {type(e).__name__}"

def worker_thread(worker_id: int, url: str, headers: Optional[Dict[str, str]] = None):
    """Worker thread that continuously sends requests to the target URL"""
    logger.info(f"Worker {worker_id} started, sending {BATCH_SIZE} requests per batch to: {url}")
    
    with create_session() as session:
        while True:
            batch_start = time.time()
            batch_success = 0
            batch_failed = 0
            
            for i in range(BATCH_SIZE):
                success, response_time, error = make_request(session, url, headers)
                
                # Update metrics
                with metrics_lock:
                    if success:
                        metrics.record_success(response_time)
                        batch_success += 1
                    else:
                        metrics.record_failure(error or "Unknown")
                        batch_failed += 1
                
                # Rate limiting
                time.sleep(REQUEST_DELAY)
            
            batch_time = time.time() - batch_start
            logger.info(
                f"Worker {worker_id} completed batch: "
                f"{batch_success} success, {batch_failed} failed, "
                f"took {batch_time:.2f}s"
            )
            
            # Pause before next batch
            time.sleep(LOOP_PAUSE_SECS)

class HealthCheckHandler(BaseHTTPRequestHandler):
    """HTTP handler for health checks and metrics"""
    
    def log_message(self, format, *args):
        """Override to use our logger"""
        logger.info(f"HTTP Request: {format % args}")
    
    def do_GET(self):
        """Handle GET requests"""
        if self.path == '/health':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            
            response = {
                'status': 'healthy',
                'active_threads': threading.active_count()
            }
            self.wfile.write(str(response).encode())
            
        elif self.path == '/metrics':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            
            with metrics_lock:
                stats = metrics.get_stats()
            
            self.wfile.write(str(stats).encode())
            
        else:
            self.send_response(404)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'Not Found. Available endpoints: /health, /metrics')

def main():
    """Main entry point"""
    # Get target URL and headers from environment
    target_url = os.environ.get("TARGET_URL")
    if not target_url:
        logger.error("TARGET_URL environment variable is required")
        return
    
    # Build headers
    headers = {}
    user_agent = os.environ.get("USER_AGENT")
    if user_agent:
        headers["User-Agent"] = user_agent
    
    forwarded_for = os.environ.get("X_FORWARDED_FOR")
    if forwarded_for:
        headers["X-Forwarded-For"] = forwarded_for
        logger.warning("X-Forwarded-For header is set - ensure this is for legitimate testing purposes")
    
    # Start worker threads
    threads = []
    logger.info(f"Starting {WORKER_THREADS} worker threads")
    
    for i in range(WORKER_THREADS):
        t = threading.Thread(
            target=worker_thread,
            args=(i + 1, target_url, headers),
            daemon=True,
            name=f"Worker-{i+1}"
        )
        t.start()
        threads.append(t)
    
    # Start HTTP server for health checks
    port = 8000
    try:
        server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
        logger.info(f"Health check server listening on port {port}")
        logger.info(f"Available endpoints: http://localhost:{port}/health, http://localhost:{port}/metrics")
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down gracefully...")
        server.shutdown()
        
        # Wait for threads to finish current work
        for t in threads:
            t.join(timeout=THREAD_JOIN_TIMEOUT)
        
        # Print final stats
        with metrics_lock:
            final_stats = metrics.get_stats()
        logger.info(f"Final statistics: {final_stats}")
        
    except Exception as e:
        logger.error(f"Server error: {e}")

if __name__ == "__main__":
    main()
        
