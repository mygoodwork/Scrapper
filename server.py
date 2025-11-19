"""
FastAPI Backend - Bybit pairs + Binance orderbooks for REAL arbitrage detection
Run: uvicorn server:app --reload
"""

import os
import asyncio
import httpx
import json
from typing import Optional, Dict, List, Set, Tuple
from enum import Enum
from dataclasses import dataclass
from collections import defaultdict
from datetime import datetime
import time

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, validator

# ============================================================================
# CONFIGURATION
# ============================================================================

BYBIT_SPOT_API_URL = "https://api.bybit.com/v5/market/tickers"
BINANCE_ORDERBOOK_URL = "https://api.binance.com/api/v3/depth"
BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/price"

TRADING_FEE = float(os.getenv("TRADING_FEE", "0.001"))  # 0.1% per trade
POPULAR_COINS = {"USDT", "USDC", "BTC", "ETH", "BNB", "SOL", "XRP", "TRX", "DOGE", "ADA", "BUSD"}
COIN_SUFFIXES = ["USDT", "USDC", "BUSD", "BTC", "ETH", "BNB", "SOL", "DOGE", "XRP", "TRX"]

ORDERBOOK_TTL = 5  # Cache orderbook for 5 seconds
MAX_ORDERBOOK_CALLS_PARALLEL = 10  # Limit concurrent orderbook requests

# Global caches
_orderbook_cache: Dict[str, Tuple[float, float, float]] = {}  # symbol -> (bid, ask, timestamp)
_http_client: Optional[httpx.AsyncClient] = None
_graph_cache: Optional['CoinGraph'] = None
_graph_timestamp: Optional[str] = None
_graph_cache_time: float = 0

# ============================================================================
# DATA MODELS
# ============================================================================

class ArbitrageMode(str, Enum):
    START_ONLY = "START_ONLY"
    POPULAR_END = "POPULAR_END"
    BOTH = "BOTH"


class RiskLevel(str, Enum):
    SAFE = "SAFE"
    MEDIUM = "MEDIUM"


class ArbitrageRequest(BaseModel):
    start_coin: str = Field(..., min_length=1, max_length=20)
    start_amount: float = Field(..., gt=0)
    mode: ArbitrageMode

    @validator('start_coin')
    def validate_coin(cls, v):
        return v.upper()


class PathOpportunity(BaseModel):
    path: List[str]
    pairs: List[str]
    start_amount: float
    end_amount: float
    profit_percent: float
    end_coin: str
    risk: RiskLevel


class ArbitrageResponse(BaseModel):
    start_coin: str
    start_amount: float
    mode: ArbitrageMode
    opportunities: List[PathOpportunity]
    total_count: int
    fetch_timestamp: str


# ============================================================================
# COIN GRAPH
# ============================================================================

@dataclass
class Edge:
    target: str
    symbol: str


class CoinGraph:
    def __init__(self):
        self.graph: Dict[str, List[Edge]] = defaultdict(list)
        self.all_coins: Set[str] = set()
        self.symbol_map: Dict[Tuple[str, str], str] = {}

    def add_pair(self, base: str, quote: str, symbol: str):
        base_u = base.upper()
        quote_u = quote.upper()
        if base_u == quote_u:
            return
        
        self.graph[base_u].append(Edge(target=quote_u, symbol=symbol))
        self.graph[quote_u].append(Edge(target=base_u, symbol=symbol))
        self.all_coins.add(base_u)
        self.all_coins.add(quote_u)
        
        self.symbol_map[(base_u, quote_u)] = symbol
        self.symbol_map[(quote_u, base_u)] = symbol

    def get_neighbors(self, coin: str) -> List[Edge]:
        return self.graph.get(coin.upper(), [])[:15]  # Limit to top 15 neighbors


# ============================================================================
# HTTP CLIENT & BINANCE ORDERBOOK
# ============================================================================

async def get_http_client() -> httpx.AsyncClient:
    """Persistent HTTP client with connection pooling."""
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            timeout=12.0,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=10)
        )
    return _http_client


async def fetch_orderbook_best(symbol: str, client: httpx.AsyncClient) -> Optional[Tuple[float, float]]:
    """
    Fetch best bid/ask from Binance orderbook with TTL caching
    Returns (best_bid, best_ask) or None if failed
    """
    now = time.time()
    
    # Check cache first
    if symbol in _orderbook_cache:
        bid, ask, ts = _orderbook_cache[symbol]
        if now - ts < ORDERBOOK_TTL:
            return bid, ask
    
    try:
        params = {"symbol": symbol, "limit": 5}
        r = await asyncio.wait_for(
            client.get(BINANCE_ORDERBOOK_URL, params=params),
            timeout=8.0
        )
        r.raise_for_status()
        data = r.json()
    except Exception:
        return None

    try:
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        if not bids or not asks:
            return None
        
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        
        _orderbook_cache[symbol] = (best_bid, best_ask, now)
        return best_bid, best_ask
    except Exception:
        return None


async def fetch_bybit_pairs() -> Dict[str, float]:
    """Fetch Bybit spot pairs with pagination (used only for coin discovery)."""
    try:
        client = await get_http_client()
        all_pairs = {}
        cursor = ""
        
        while True:
            params = {"category": "spot", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            
            r = await asyncio.wait_for(
                client.get(BYBIT_SPOT_API_URL, params=params),
                timeout=10.0
            )
            r.raise_for_status()
            data = r.json()
            
            if data.get("retCode") != 0:
                break
            
            tickers = data.get("result", {}).get("list", [])
            if not tickers:
                break
            
            for ticker in tickers:
                try:
                    symbol = ticker.get("symbol", "")
                    # Convert Bybit format (BTCUSDT) to Binance format
                    binance_symbol = symbol.replace("_", "")
                    all_pairs[binance_symbol] = 1.0
                except Exception:
                    continue
            
            next_cursor = data.get("result", {}).get("nextPageCursor", "")
            if not next_cursor:
                break
            cursor = next_cursor
        
        return all_pairs
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Bybit fetch failed: {str(e)}")


def extract_coins(symbol: str) -> Tuple[Optional[str], Optional[str]]:
    """Extract base/quote from symbol."""
    symbol_u = symbol.upper()
    for suffix in COIN_SUFFIXES:
        if symbol_u.endswith(suffix) and len(symbol_u) > len(suffix):
            return symbol_u[:-len(suffix)], suffix
    return None, None


def build_graph(symbols: Dict[str, float]) -> CoinGraph:
    """Build coin graph from symbols."""
    graph = CoinGraph()
    for sym in symbols.keys():
        base, quote = extract_coins(sym)
        if base and quote and base != quote:
            graph.add_pair(base, quote, sym)
    return graph


# ============================================================================
# DFS - HEAVILY OPTIMIZED
# ============================================================================

POPULAR_COINS_SET = POPULAR_COINS

def get_risk(coin: str) -> RiskLevel:
    return RiskLevel.SAFE if coin.upper() in POPULAR_COINS_SET else RiskLevel.MEDIUM


def find_paths(graph: CoinGraph, start: str, mode: ArbitrageMode, max_paths: int = 200) -> List[List[str]]:
    """Aggressive path pruning - max 200 paths, depth 3 only"""
    start = start.upper()
    paths: List[List[str]] = []
    seen = set()

    def dfs(curr: str, path: List[str]):
        if len(paths) >= max_paths:
            return
        if len(path) == 3:
            end = path[-1]
            ok = False
            if mode == ArbitrageMode.START_ONLY:
                ok = end == start
            elif mode == ArbitrageMode.POPULAR_END:
                ok = end in POPULAR_COINS_SET
            else:
                ok = True
            if ok and tuple(path) not in seen:
                seen.add(tuple(path))
                paths.append(path[:])
            return
        
        for edge in graph.get_neighbors(curr):
            if len(path) <= 1 or edge.target != path[-2]:
                dfs(edge.target, path + [edge.target])

    dfs(start, [start])
    return paths


# ============================================================================
# PROFIT CALCULATION WITH ORDERBOOKS
# ============================================================================

async def calc_profit_with_ob(
    graph: CoinGraph,
    path: List[str],
    start_amt: float,
    ob_cache: Dict[str, Tuple[float, float]]
) -> Optional[PathOpportunity]:
    """
    Calculate profit using Binance orderbook prices (bid/ask)
    Returns opportunity or None if invalid
    """
    amt = start_amt
    pairs_used = []

    for i in range(len(path) - 1):
        src = path[i].upper()
        tgt = path[i + 1].upper()
        
        symbol = graph.symbol_map.get((src, tgt))
        if not symbol:
            return None
        
        # Get bid/ask from cache or fetch
        if symbol in ob_cache:
            best_bid, best_ask = ob_cache[symbol]
        else:
            client = await get_http_client()
            result = await fetch_orderbook_best(symbol, client)
            if not result:
                return None
            best_bid, best_ask = result
            ob_cache[symbol] = best_bid, best_ask
        
        # Parse symbol to determine direction
        base, quote = extract_coins(symbol)
        if not base or not quote:
            return None
        
        # Determine direction and apply bid/ask accordingly
        if src == quote and tgt == base:
            # Selling quote, buying base -> use best_ask
            amt = (amt / best_ask) * (1 - TRADING_FEE)
        elif src == base and tgt == quote:
            # Selling base, buying quote -> use best_bid
            amt = (amt * best_bid) * (1 - TRADING_FEE)
        else:
            return None
        
        pairs_used.append(symbol)
        
        if amt < 0.0001:
            return None
    
    profit_pct = ((amt - start_amt) / start_amt) * 100
    if profit_pct <= 0:
        return None
    
    return PathOpportunity(
        path=path,
        pairs=pairs_used,
        start_amount=start_amt,
        end_amount=round(amt, 12),
        profit_percent=round(profit_pct, 8),
        end_coin=path[-1],
        risk=get_risk(path[-1])
    )


# ============================================================================
# FASTAPI
# ============================================================================

app = FastAPI(title="Arbitrage Backend", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


async def get_or_refresh_graph(force: bool = False) -> Tuple[CoinGraph, str]:
    """Get cached graph or refresh (5 min TTL)."""
    global _graph_cache, _graph_timestamp, _graph_cache_time
    now = time.time()
    
    if force or _graph_cache is None or (now - _graph_cache_time) > 300:
        pairs = await fetch_bybit_pairs()
        _graph_cache = build_graph(pairs)
        _graph_timestamp = datetime.utcnow().isoformat()
        _graph_cache_time = now
    
    return _graph_cache, _graph_timestamp


@app.on_event("startup")
async def startup():
    """Warm cache on startup."""
    await get_or_refresh_graph(force=True)


@app.on_event("shutdown")
async def shutdown():
    """Close client."""
    global _http_client
    if _http_client:
        await _http_client.aclose()


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/graph/info")
async def graph_info():
    graph, ts = await get_or_refresh_graph()
    return {"coins": len(graph.all_coins), "timestamp": ts}


@app.post("/arbitrage/calculate", response_model=ArbitrageResponse)
async def calculate(request: ArbitrageRequest):
    """Fast calculation with orderbook prices (target: <3 seconds)"""
    try:
        start = time.time()
        graph, ts = await get_or_refresh_graph()
        
        start_coin = request.start_coin.upper()
        if start_coin not in graph.all_coins:
            raise HTTPException(400, f"Coin {start_coin} not found")
        
        # Find paths
        paths = find_paths(graph, start_coin, request.mode, max_paths=200)
        
        # Prefetch all orderbooks in parallel
        symbols_needed = set()
        for path in paths:
            for i in range(len(path) - 1):
                sym = graph.symbol_map.get((path[i].upper(), path[i + 1].upper()))
                if sym:
                    symbols_needed.add(sym)
        
        ob_cache: Dict[str, Tuple[float, float]] = {}
        client = await get_http_client()
        
        # Batch fetch with semaphore for parallelism
        sem = asyncio.Semaphore(MAX_ORDERBOOK_CALLS_PARALLEL)
        async def fetch_with_sem(sym):
            async with sem:
                result = await fetch_orderbook_best(sym, client)
                if result:
                    ob_cache[sym] = result
        
        await asyncio.gather(*[fetch_with_sem(sym) for sym in symbols_needed], return_exceptions=True)
        
        # Calculate profits
        opportunities = []
        tasks = [calc_profit_with_ob(graph, path, request.start_amount, ob_cache) for path in paths]
        
        for coro in asyncio.as_completed(tasks):
            try:
                opp = await coro
                if opp:
                    opportunities.append(opp)
            except Exception:
                continue
        
        # Sort and limit
        opportunities.sort(key=lambda x: x.profit_percent, reverse=True)
        
        elapsed = time.time() - start
        print(f"[v0] Calculation took {elapsed:.2f}s for {len(paths)} paths, found {len(opportunities)} opportunities")
        
        return ArbitrageResponse(
            start_coin=start_coin,
            start_amount=request.start_amount,
            mode=request.mode,
            opportunities=opportunities[:50],
            total_count=len(opportunities),
            fetch_timestamp=ts
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/arbitrage/refresh")
async def refresh():
    """Refresh graph."""
    try:
        pairs = await fetch_bybit_pairs()
        global _graph_cache, _graph_timestamp, _graph_cache_time
        _graph_cache = build_graph(pairs)
        _graph_timestamp = datetime.utcnow().isoformat()
        _graph_cache_time = time.time()
        return {"status": "ok", "timestamp": _graph_timestamp}
    except Exception as e:
        raise HTTPException(500, str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
    
