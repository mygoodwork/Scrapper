"""
FastAPI Backend using real Binance orderbooks for 4-step circular arbitrage detection (streaming + async parallel)
Run: uvicorn server:app --reload
"""

import os
import httpx
import asyncio
from typing import Optional, Dict, List, Set, Tuple
from enum import Enum
from dataclasses import dataclass
from collections import defaultdict
from datetime import datetime
import json

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, validator

# ----------------------------
# Config
# ----------------------------
BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/price"
BINANCE_ORDERBOOK_URL = "https://api.binance.com/api/v3/depth"
ORDERBOOK_LIMIT = 500
TRADING_FEE = float(os.getenv("TRADING_FEE", "0.001"))
QUOTE_COINS = ["USDT", "BUSD", "USDC", "BTC", "ETH", "BNB", "SOL", "DOGE", "TRX", "XRP", "ADA"]
ORDERBOOK_TTL_SECONDS = 10
MIN_PROFIT_PERCENT = 0.1

# ----------------------------
# Models
# ----------------------------
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

    @validator("start_coin")
    def upper_coin(cls, v):
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

# ----------------------------
# Graph structures
# ----------------------------
@dataclass
class Edge:
    target: str
    pair_symbol: str
    base: str
    quote: str

class CoinGraph:
    def __init__(self):
        self.graph: Dict[str, List[Edge]] = defaultdict(list)
        self.all_coins: Set[str] = set()
        self.symbol_map: Dict[Tuple[str, str], Tuple[str, str, str]] = {}  # (base_upper, quote_upper) -> (symbol, base, quote)

    def add_pair(self, base: str, quote: str, symbol: str):
        base_u = base.upper()
        quote_u = quote.upper()
        if base_u == quote_u:
            return
        self.graph[base_u].append(Edge(target=quote_u, pair_symbol=symbol, base=base_u, quote=quote_u))
        self.graph[quote_u].append(Edge(target=base_u, pair_symbol=symbol, base=quote_u, quote=base_u))
        self.all_coins.add(base_u)
        self.all_coins.add(quote_u)
        self.symbol_map[(base_u, quote_u)] = (symbol, base_u, quote_u)
        self.symbol_map[(quote_u, base_u)] = (symbol, quote_u, base_u)

    def get_neighbors(self, coin: str) -> List[Edge]:
        return self.graph.get(coin.upper(), [])

# ----------------------------
# Binance helpers
# ----------------------------
async def fetch_all_binance_symbols() -> Dict[str, Tuple[str, str]]:
    """
    Returns dict of symbol -> (base, quote) pairs instead of just symbol -> price.
    This is obtained by intelligently parsing Binance symbol naming conventions.
    """
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get("https://api.binance.com/api/v3/exchangeInfo")
        r.raise_for_status()
        data = r.json()
    
    pairs: Dict[str, Tuple[str, str]] = {}
    for symbol_obj in data.get("symbols", []):
        if symbol_obj.get("status") != "TRADING":
            continue
        symbol = symbol_obj.get("symbol", "")
        base = symbol_obj.get("baseAsset", "")
        quote = symbol_obj.get("quoteAsset", "")
        if symbol and base and quote:
            pairs[symbol] = (base, quote)
    
    print(f"[v0] Fetched {len(pairs)} Binance trading pairs from exchangeInfo")
    return pairs

_orderbook_global_cache: Dict[str, Tuple[float, float, float, float, datetime]] = {}

async def fetch_orderbook_best(symbol: str, client: httpx.AsyncClient) -> Optional[Tuple[float, float, float, float]]:
    """
    Fetch orderbook and return (best_bid, best_ask, weighted_bid, weighted_ask).
    Weighted prices are based on cumulative volume in first 5% of orderbook depth.
    """
    now = datetime.utcnow()
    cached = _orderbook_global_cache.get(symbol)
    if cached and (now - cached[4]).total_seconds() < ORDERBOOK_TTL_SECONDS:
        return cached[0], cached[1], cached[2], cached[3]

    params = {"symbol": symbol, "limit": ORDERBOOK_LIMIT}
    try:
        r = await client.get(BINANCE_ORDERBOOK_URL, params=params)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[v0] Failed to fetch orderbook for {symbol}: {e}")
        return None

    bids = data.get("bids", [])
    asks = data.get("asks", [])
    if not bids or not asks:
        return None
    
    try:
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        
        bid_total_volume = sum(float(b[1]) for b in bids)
        weighted_bid_threshold = bid_total_volume * 0.05
        bid_cumulative = 0
        weighted_bid = best_bid
        
        for bid_price, bid_volume in bids:
            bid_volume_f = float(bid_volume)
            bid_cumulative += bid_volume_f
            weighted_bid = float(bid_price)
            if bid_cumulative >= weighted_bid_threshold:
                break
        
        ask_total_volume = sum(float(a[1]) for a in asks)
        weighted_ask_threshold = ask_total_volume * 0.05
        ask_cumulative = 0
        weighted_ask = best_ask
        
        for ask_price, ask_volume in asks:
            ask_volume_f = float(ask_volume)
            ask_cumulative += ask_volume_f
            weighted_ask = float(ask_price)
            if ask_cumulative >= weighted_ask_threshold:
                break
        
        _orderbook_global_cache[symbol] = (best_bid, best_ask, weighted_bid, weighted_ask, now)
        return best_bid, best_ask, weighted_bid, weighted_ask
    except Exception as e:
        print(f"[v0] Error parsing orderbook for {symbol}: {e}")
        return None

# ----------------------------
# Build graph from symbols
# ----------------------------
def build_graph_from_symbols(symbols: Dict[str, Tuple[str, str]]) -> CoinGraph:
    """
    Now receives proper base/quote pairs from exchangeInfo instead of trying to parse them.
    This eliminates ambiguity (e.g., is BNBUSDT BNB/USDT or BNU/BUST?).
    All Binance pairs are included without QUOTE_COINS filtering.
    """
    graph = CoinGraph()
    for symbol, (base, quote) in symbols.items():
        graph.add_pair(base, quote, symbol)
    
    print(f"[v0] Built graph with {len(graph.all_coins)} coins and {sum(len(e) for e in graph.graph.values())} edges")
    return graph

# ----------------------------
# DFS paths (4-step circular arbitrage only)
# ----------------------------
POPULAR_COINS = {"USDT", "USDC", "BTC", "ETH", "BNB", "SOL", "XRP", "TRX", "DOGE"}

def get_risk_level(coin: str) -> RiskLevel:
    return RiskLevel.SAFE if coin.upper() in POPULAR_COINS else RiskLevel.MEDIUM

def find_paths_dfs(
    graph: CoinGraph,
    start_coin: str,
    mode: ArbitrageMode,
    max_paths: int = 300
) -> List[List[str]]:
    start = start_coin.upper()
    paths: List[List[str]] = []
    seen = set()
    
    def sort_neighbors(edges: List[Edge]) -> List[Edge]:
        return sorted(edges, key=lambda e: 0 if e.target in POPULAR_COINS else 1)

    def dfs(current: str, path: List[str]):
        if len(paths) >= max_paths:
            return
        
        if len(path) == 4:
            end = path[-1]
            if end == start:
                t = tuple(path)
                if t not in seen:
                    seen.add(t)
                    paths.append(path.copy())
            return
        
        neighbors = sort_neighbors(graph.get_neighbors(current))
        for edge in neighbors[:15]:
            if len(path) == 3:
                if edge.target == start:
                    path.append(edge.target)
                    dfs(edge.target, path)
                    path.pop()
            else:
                if len(path) <= 1 or edge.target != path[-2]:
                    path.append(edge.target)
                    dfs(edge.target, path)
                    path.pop()

    dfs(start, [start])
    print(f"[v0] Found {len(paths)} paths from {start}")
    return paths

# ----------------------------
# Async path profit simulation - FIXED
# ----------------------------
def simulate_path_profit_sync(
    graph: CoinGraph,
    path: List[str],
    start_amount: float,
    orderbook_cache: Dict[str, Tuple[float, float, float, float]]
) -> Optional[PathOpportunity]:
    """
    Completely rewritten to use proper base/quote from graph edges.
    Now correctly handles multi-leg trades without QUOTE_COINS filtering.
    Uses weighted average prices; no trading fees applied.
    """
    amount = start_amount
    pairs_used: List[str] = []

    for i in range(len(path) - 1):
        source = path[i].upper()
        target = path[i + 1].upper()
        
        edge = None
        for e in graph.get_neighbors(source):
            if e.target == target:
                edge = e
                break
        
        if not edge:
            return None
        
        symbol = edge.pair_symbol
        
        if symbol not in orderbook_cache:
            return None
        
        best_bid, best_ask, weighted_bid, weighted_ask = orderbook_cache[symbol]

        # If source is base, we're buying quote with base (multiply by bid)
        # If source is quote, we're selling quote for base (divide by ask)
        if source == edge.base and target == edge.quote:
            # Trading base for quote: amount * bid price
            amount = amount * weighted_bid
        elif source == edge.quote and target == edge.base:
            # Trading quote for base: amount / ask price
            amount = amount / weighted_ask
        else:
            print(f"[v0] Direction mismatch for {symbol}: source={source}, target={target}, edge.base={edge.base}, edge.quote={edge.quote}")
            return None

        pairs_used.append(symbol)

    if amount <= start_amount * 1.0001:  # Allow tiny profit threshold
        return None
    
    profit_percent = ((amount - start_amount) / start_amount) * 100
    
    if profit_percent < MIN_PROFIT_PERCENT:
        return None
    
    return PathOpportunity(
        path=path,
        pairs=pairs_used,
        start_amount=start_amount,
        end_amount=round(amount, 12),
        profit_percent=round(profit_percent, 8),
        end_coin=path[-1],
        risk=get_risk_level(path[-1])
    )

# ----------------------------
# FastAPI app
# ----------------------------
app = FastAPI(title="Binance 4-Step Circular Arbitrage", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://scrapper-h4xe.onrender.com",
        "https://arbitragecruo.netlify.app"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_graph_cache: Optional[CoinGraph] = None
_graph_timestamp: Optional[str] = None
_last_symbol_snapshot: Optional[Dict[str, Tuple[str, str]]] = None

async def get_or_refresh_graph(force: bool = False) -> Tuple[CoinGraph, str]:
    global _graph_cache, _graph_timestamp, _last_symbol_snapshot
    if _graph_cache is None or force or _last_symbol_snapshot is None:
        symbols = await fetch_all_binance_symbols()
        _last_symbol_snapshot = symbols
        _graph_cache = build_graph_from_symbols(symbols)
        _graph_timestamp = datetime.utcnow().isoformat()
    return _graph_cache, _graph_timestamp

@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "binance-arb-backend"}

@app.get("/graph/info")
async def graph_info():
    graph, timestamp = await get_or_refresh_graph()
    return {
        "coins_count": len(graph.all_coins),
        "pairs_count": sum(len(edges) for edges in graph.graph.values()) // 2,  # Divide by 2 since bidirectional
        "fetch_timestamp": timestamp,
    }

@app.post("/arbitrage/refresh")
async def refresh_graph():
    graph, timestamp = await get_or_refresh_graph(force=True)
    return {"status": "success", "fetch_timestamp": timestamp, "coins_count": len(graph.all_coins)}

@app.post("/arbitrage/calculate/stream")
async def calculate_arbitrage_stream(request: ArbitrageRequest, limit: int = Query(300, ge=1, le=1000)):
    graph, fetch_timestamp = await get_or_refresh_graph()
    start_coin = request.start_coin.upper()
    if start_coin not in graph.all_coins:
        raise HTTPException(status_code=400, detail=f"Start coin '{start_coin}' not available in Binance pairs")

    candidate_paths = find_paths_dfs(graph, start_coin, request.mode, max_paths=min(limit, 300))
    print(f"[v0] Scanning {len(candidate_paths)} paths for {start_coin}")

    symbols_needed: Set[str] = set()
    for path in candidate_paths:
        for i in range(len(path) - 1):
            s, t = path[i].upper(), path[i + 1].upper()
            for edge in graph.get_neighbors(s):
                if edge.target == t:
                    symbols_needed.add(edge.pair_symbol)
                    break

    orderbook_cache: Dict[str, Tuple[float, float, float, float]] = {}
    semaphore = asyncio.Semaphore(50)
    
    async with httpx.AsyncClient(timeout=8.0) as client:
        async def fetch_sym(sym):
            async with semaphore:
                try:
                    best = await fetch_orderbook_best(sym, client)
                    if best:
                        orderbook_cache[sym] = best
                except Exception:
                    pass
        
        try:
            await asyncio.wait_for(
                asyncio.gather(*(fetch_sym(sym) for sym in symbols_needed), return_exceptions=True),
                timeout=5.0
            )
        except asyncio.TimeoutError:
            pass
    
    print(f"[v0] Fetched orderbooks for {len(orderbook_cache)} symbols")

    async def generator():
        opportunities = []
        
        for path in candidate_paths:
            res = simulate_path_profit_sync(graph, path, request.start_amount, orderbook_cache)
            if res:
                opportunities.append(res)
        
        opportunities.sort(key=lambda x: x.profit_percent, reverse=True)
        opportunities = opportunities[:50]
        print(f"[v0] Found {len(opportunities)} profitable opportunities for {start_coin}")
        
        yield '{"start_coin": "%s", "start_amount": %f, "mode": "%s", "opportunities": [' % (
            start_coin, request.start_amount, request.mode
        )
        
        for idx, opp in enumerate(opportunities):
            if idx > 0:
                yield ","
            yield json.dumps(opp.dict())
        
        yield '], "total_count": %d, "fetch_timestamp": "%s"}' % (len(opportunities), fetch_timestamp)

    response = StreamingResponse(generator(), media_type="application/json")
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Credentials"] = "true"
    response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response

@app.get("/arbitrage/scan/stream")
async def scan_all_arbitrage_stream(
    start_amount: float = Query(1000, gt=0),
    limit: int = Query(50, ge=1, le=200)
):
    """
    Automatically scan ALL circular arbitrage opportunities across all popular coins.
    """
    graph, fetch_timestamp = await get_or_refresh_graph()
    
    start_coins = list(POPULAR_COINS)
    all_paths = []
    
    for start_coin in start_coins:
        if start_coin not in graph.all_coins:
            continue
        paths = find_paths_dfs(graph, start_coin, ArbitrageMode.START_ONLY, max_paths=100)
        all_paths.extend(paths)
    
    unique_paths = []
    seen = set()
    for path in all_paths:
        t = tuple(path)
        if t not in seen:
            seen.add(t)
            unique_paths.append(path)
    
    print(f"[v0] Total unique paths to scan: {len(unique_paths)}")
    
    symbols_needed: Set[str] = set()
    for path in unique_paths:
        for i in range(len(path) - 1):
            s, t = path[i].upper(), path[i + 1].upper()
            for edge in graph.get_neighbors(s):
                if edge.target == t:
                    symbols_needed.add(edge.pair_symbol)
                    break
    
    orderbook_cache: Dict[str, Tuple[float, float, float, float]] = {}
    semaphore = asyncio.Semaphore(50)
    
    async with httpx.AsyncClient(timeout=8.0) as client:
        async def fetch_sym(sym):
            async with semaphore:
                try:
                    best = await fetch_orderbook_best(sym, client)
                    if best:
                        orderbook_cache[sym] = best
                except Exception:
                    pass
        
        try:
            await asyncio.wait_for(
                asyncio.gather(*(fetch_sym(sym) for sym in symbols_needed), return_exceptions=True),
                timeout=5.0
            )
        except asyncio.TimeoutError:
            pass
    
    print(f"[v0] Fetched orderbooks for {len(orderbook_cache)} symbols out of {len(symbols_needed)}")
    
    async def generator():
        opportunities = []
        
        for path in unique_paths:
            res = simulate_path_profit_sync(graph, path, start_amount, orderbook_cache)
            if res:
                opportunities.append(res)
        
        opportunities.sort(key=lambda x: x.profit_percent, reverse=True)
        opportunities = opportunities[:limit]
        print(f"[v0] Found {len(opportunities)} opportunities after filtering")
        
        yield '{"opportunities": ['
        
        for idx, opp in enumerate(opportunities):
            if idx > 0:
                yield ","
            yield json.dumps(opp.dict())
        
        yield '], "total_count": %d, "fetch_timestamp": "%s"}' % (len(opportunities), fetch_timestamp)
    
    response = StreamingResponse(generator(), media_type="application/json")
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Credentials"] = "true"
    response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response

# ----------------------------
# Run
# ----------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=True)
        
