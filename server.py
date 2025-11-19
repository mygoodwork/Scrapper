"""
FastAPI Backend using real Binance orderbooks for arbitrage detection (streaming + async parallel)
Run: uvicorn server:app --reload
"""

import os
import httpx
import asyncio
from typing import Optional, Dict, List, Set, Tuple
from enum import Enum
from dataclasses import dataclass
from collections import defaultdict
from datetime import datetime, timedelta
import json

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, validator

# ----------------------------
# Config
# ----------------------------
BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/price"
BINANCE_ORDERBOOK_URL = "https://api.binance.com/api/v3/depth"  # ?symbol=SYMBOL&limit=5
ORDERBOOK_LIMIT = 5
TRADING_FEE = float(os.getenv("TRADING_FEE", "0.001"))
QUOTE_COINS = ["USDT", "BUSD", "USDC", "BTC", "ETH", "BNB", "SOL", "DOGE", "TRX", "XRP", "ADA"]
ORDERBOOK_TTL_SECONDS = 2  # cache for 2s

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
        self.graph[base_u].append(Edge(target=quote_u, pair_symbol=symbol))
        self.graph[quote_u].append(Edge(target=base_u, pair_symbol=symbol))
        self.all_coins.add(base_u)
        self.all_coins.add(quote_u)
        self.symbol_map[(base_u, quote_u)] = symbol
        self.symbol_map[(quote_u, base_u)] = symbol

    def get_neighbors(self, coin: str) -> List[Edge]:
        return self.graph.get(coin.upper(), [])

# ----------------------------
# Binance helpers
# ----------------------------
async def fetch_all_binance_symbols() -> Dict[str, float]:
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(BINANCE_TICKER_URL)
        r.raise_for_status()
        data = r.json()
    pairs: Dict[str, float] = {}
    for entry in data:
        sym = entry.get("symbol")
        try:
            price = float(entry.get("price", "0"))
            if price > 0:
                pairs[sym] = price
        except Exception:
            continue
    return pairs

# Global orderbook cache with TTL
_orderbook_global_cache: Dict[str, Tuple[float, float, datetime]] = {}

async def fetch_orderbook_best(symbol: str, client: httpx.AsyncClient) -> Optional[Tuple[float, float]]:
    now = datetime.utcnow()
    cached = _orderbook_global_cache.get(symbol)
    if cached and (now - cached[2]).total_seconds() < ORDERBOOK_TTL_SECONDS:
        return cached[0], cached[1]

    params = {"symbol": symbol, "limit": ORDERBOOK_LIMIT}
    try:
        r = await client.get(BINANCE_ORDERBOOK_URL, params=params)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return None

    bids = data.get("bids", [])
    asks = data.get("asks", [])
    if not bids or not asks:
        return None
    try:
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        _orderbook_global_cache[symbol] = (best_bid, best_ask, now)
        return best_bid, best_ask
    except Exception:
        return None

# ----------------------------
# Build graph from symbols
# ----------------------------
def build_graph_from_symbols(symbols: Dict[str, float]) -> CoinGraph:
    graph = CoinGraph()
    for sym in symbols.keys():
        sym_u = sym.upper()
        for q in QUOTE_COINS:
            if sym_u.endswith(q) and len(sym_u) > len(q):
                base = sym_u[: len(sym_u) - len(q)]
                quote = q
                if base and base != quote:
                    graph.add_pair(base, quote, sym_u)
                break
    return graph

# ----------------------------
# DFS paths
# ----------------------------
POPULAR_COINS = {"USDT", "USDC", "BTC", "ETH", "BNB", "SOL", "XRP", "TRX", "DOGE"}

def get_risk_level(coin: str) -> RiskLevel:
    return RiskLevel.SAFE if coin.upper() in POPULAR_COINS else RiskLevel.MEDIUM

def find_paths_dfs(
    graph: CoinGraph,
    start_coin: str,
    mode: ArbitrageMode,
    max_depth: int = 4,
    max_paths: int = 500
) -> List[List[str]]:
    start = start_coin.upper()
    paths: List[List[str]] = []
    seen = set()

    def dfs(current: str, path: List[str], depth: int):
        if len(paths) >= max_paths:
            return
        if len(path) >= 3:
            end = path[-1]
            ok = False
            if mode == ArbitrageMode.START_ONLY:
                ok = end == start
            elif mode == ArbitrageMode.POPULAR_END:
                ok = end.upper() in POPULAR_COINS
            else:
                ok = True
            if ok:
                t = tuple(path)
                if t not in seen:
                    seen.add(t)
                    paths.append(path.copy())
        if depth < max_depth:
            for edge in graph.get_neighbors(current):
                if len(path) <= 1 or edge.target != path[-2]:
                    path.append(edge.target)
                    dfs(edge.target, path, depth + 1)
                    path.pop()

    dfs(start, [start], 0)
    return paths

# ----------------------------
# Async path profit simulation (with prefetch cache)
# ----------------------------
async def simulate_path_profit_with_cache(
    graph: CoinGraph,
    path: List[str],
    start_amount: float,
    orderbook_cache: Dict[str, Tuple[float, float]]
) -> Optional[PathOpportunity]:
    amount = start_amount
    pairs_used: List[str] = []

    for i in range(len(path) - 1):
        source = path[i].upper()
        target = path[i + 1].upper()
        symbol = graph.symbol_map.get((source, target))
        if not symbol:
            return None

        # Fetch from cache or fresh
        if symbol in orderbook_cache:
            best_bid, best_ask = orderbook_cache[symbol]
        else:
            async with httpx.AsyncClient(timeout=10.0) as client:
                best = await fetch_orderbook_best(symbol, client)
                if not best:
                    return None
                best_bid, best_ask = best
                orderbook_cache[symbol] = best_bid, best_ask

        # Identify base/quote
        base, quote = None, None
        for q in QUOTE_COINS:
            if symbol.upper().endswith(q) and len(symbol) > len(q):
                base = symbol[: len(symbol) - len(q)]
                quote = q
                break
        if not base or not quote:
            return None

        # Conversion
        if source == quote and target == base:
            amount = (amount / best_ask) * (1 - TRADING_FEE)
        elif source == base and target == quote:
            amount = (amount * best_bid) * (1 - TRADING_FEE)
        else:
            return None

        pairs_used.append(symbol)

    profit_percent = ((amount - start_amount) / start_amount) * 100
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
app = FastAPI(title="Binance Orderbook Arbitrage", version="1.0.0")
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
_last_symbol_snapshot: Optional[Dict[str, float]] = None

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
        "pairs_count": sum(len(edges) for edges in graph.graph.values()),
        "fetch_timestamp": timestamp,
    }

@app.post("/arbitrage/refresh")
async def refresh_graph():
    graph, timestamp = await get_or_refresh_graph(force=True)
    return {"status": "success", "fetch_timestamp": timestamp, "coins_count": len(graph.all_coins)}

@app.post("/arbitrage/calculate/stream")
async def calculate_arbitrage_stream(request: ArbitrageRequest, limit: int = Query(500, ge=1, le=5000)):
    graph, fetch_timestamp = await get_or_refresh_graph()
    start_coin = request.start_coin.upper()
    if start_coin not in graph.all_coins:
        raise HTTPException(status_code=400, detail=f"Start coin '{start_coin}' not available in Binance pairs")

    candidate_paths = find_paths_dfs(graph, start_coin, request.mode, max_depth=4, max_paths=limit)

    # Prefetch all symbols
    symbols_needed: Set[str] = set()
    for path in candidate_paths:
        for i in range(len(path) - 1):
            s, t = path[i].upper(), path[i + 1].upper()
            sym = graph.symbol_map.get((s, t))
            if sym:
                symbols_needed.add(sym)

    orderbook_cache: Dict[str, Tuple[float, float]] = {}
    async with httpx.AsyncClient(timeout=15.0) as client:
        async def fetch_sym(sym):
            best = await fetch_orderbook_best(sym, client)
            if best:
                orderbook_cache[sym] = best
        await asyncio.gather(*(fetch_sym(sym) for sym in symbols_needed))

    # Streaming generator
    async def generator():
        yield '{"start_coin": "%s", "start_amount": %f, "mode": "%s", "opportunities": [' % (
            start_coin, request.start_amount, request.mode
        )
        first = True
        tasks = [simulate_path_profit_with_cache(graph, path, request.start_amount, orderbook_cache) for path in candidate_paths]
        for coro in asyncio.as_completed(tasks):
            res = await coro
            if res and res.profit_percent > 0:
                if not first:
                    yield ","
                else:
                    first = False
                yield json.dumps(res.dict())
        yield '], "total_count": null, "fetch_timestamp": "%s"}' % fetch_timestamp

    response = StreamingResponse(generator(), media_type="application/json")
    # Explicit CORS headers for streaming response
    response.headers["Access-Control-Allow-Origin"] = "https://arbitragecruo.netlify.app"
    response.headers["Access-Control-Allow-Credentials"] = "true"
    response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"

    return response

# ----------------------------
# Run
# ----------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=True)
