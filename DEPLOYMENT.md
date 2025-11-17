# Crypto Arbitrage Backend - Deployment Guide

## Quick Start

### 1. Install Dependencies
\`\`\`bash
pip install -r requirements.txt
\`\`\`

### 2. Run the Server
\`\`\`bash
uvicorn server:app --reload
\`\`\`

The server will start at `http://localhost:8000`

### 3. Access API Documentation
- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

## API Endpoints

### Health Check
\`\`\`bash
GET /health
\`\`\`

### Graph Information
\`\`\`bash
GET /graph/info
\`\`\`
Returns information about the current coin graph (coins count, pairs count, timestamp).

### Calculate Arbitrage Opportunities
\`\`\`bash
POST /arbitrage/calculate
Content-Type: application/json

{
  "start_coin": "USDT",
  "start_amount": 1000,
  "mode": "START_ONLY|POPULAR_END|BOTH"
}
\`\`\`

**Modes:**
- `START_ONLY`: Paths must end in the starting coin (triangular arbitrage)
- `POPULAR_END`: Paths must end in major coins (USDT, USDC, BTC, ETH, BNB, SOL, XRP, TRX, DOGE)
- `BOTH`: Any end coin allowed

**Response Example:**
\`\`\`json
{
  "start_coin": "USDT",
  "start_amount": 1000,
  "mode": "START_ONLY",
  "opportunities": [
    {
      "path": ["USDT", "BTC", "ETH", "USDT"],
      "start_amount": 1000,
      "end_amount": 1004.2,
      "profit_percent": 0.42,
      "end_coin": "USDT",
      "risk": "SAFE"
    }
  ],
  "total_count": 15,
  "fetch_timestamp": "2025-11-17T12:34:56.789123"
}
\`\`\`

### Refresh Graph
\`\`\`bash
POST /arbitrage/refresh
\`\`\`
Manually refresh the coin graph from Bybit's API.

## Architecture

### CoinGraph
- Bidirectional graph where nodes are coins and edges are trading pairs
- Each pair is stored in both directions: A→B at price P, B→A at price 1/P
- Supports efficient neighbor lookup for DFS traversal

### DFS Path Finding
- Finds arbitrage paths up to 4 legs long (triangular, quadrangular)
- Avoids immediate loops but allows coin revisits
- Limits to 500 paths per request to avoid timeouts
- Filters paths based on selected mode

### Profit Calculation
- Applies 0.1% trading fee per leg
- Formula: `end_amount = start_amount * ∏(price_i * (1 - 0.001))`
- Calculates profit percentage relative to starting amount

### Risk Assessment
- SAFE: Popular coins (USDT, USDC, BTC, ETH, BNB, SOL, XRP, TRX, DOGE)
- MEDIUM: All other coins

## Configuration

### Constants (in server.py)
- `BYBIT_SPOT_API_URL`: Bybit v5 API endpoint
- `TRADING_FEE`: 0.1% per trade (0.001)
- `POPULAR_COINS`: Set of coins for SAFE risk classification
- `max_depth`: Maximum path length (default: 4)
- `max_paths`: Maximum paths to find (default: 500)

## Error Handling

- Invalid coin: Returns 400 with "Coin not found in Bybit spot pairs"
- API errors: Returns 500 with detailed error message
- All endpoints include CORS headers for frontend integration

## Performance Notes

- Graph is cached after first load (~5-30 seconds depending on Bybit API)
- Subsequent requests are instant until `/arbitrage/refresh` is called
- DFS with max_depth=4 and max_paths=500 typically completes in <2 seconds
- Bybit API pagination handles 1000+ trading pairs automatically

## Environment

No environment variables required. The backend uses Bybit's public API.

## Production Deployment

For production, use a production ASGI server:

\`\`\`bash
pip install gunicorn
gunicorn -w 4 -k uvicorn.workers.UvicornWorker server:app
\`\`\`

Or with uvicorn directly:
\`\`\`bash
uvicorn server:app --host 0.0.0.0 --port 8000 --workers 4
