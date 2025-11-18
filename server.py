"""
FastAPI Backend for Manual Crypto Arbitrage System (v0)
Fetches all spot pairs from Binance, builds a coin graph, and finds arbitrage opportunities.
Run: uvicorn server:app --reload
"""

import asyncio
import os
import httpx
from typing import Optional, Dict, List, Set, Tuple
from enum import Enum
from dataclasses import dataclass
from collections import defaultdict
from datetime import datetime

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, validator

# ============================================================================
# Data Models and Enums
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
    def validate_start_coin(cls, v):
        return v.upper()

class PathOpportunity(BaseModel):
    path: List[str]
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
# Coin Graph
# ============================================================================

@dataclass
class Edge:
    target: str
    price: float

class CoinGraph:
    def __init__(self):
        self.graph: Dict[str, List[Edge]] = defaultdict(list)
        self.all_coins: Set[str] = set()

    def add_edge(self, source: str, target: str, price: float):
        self.graph[source].append(Edge(target=target, price=price))
        self.all_coins.add(source)
        self.all_coins.add(target)

    def add_pair(self, coin1: str, coin2: str, price: float):
        self.add_edge(coin1, coin2, price)
        if price > 0:
            self.add_edge(coin2, coin1, 1 / price)

    def get_neighbors(self, coin: str) -> List[Edge]:
        return self.graph.get(coin, [])

    def coins_count(self) -> int:
        return len(self.all_coins)

    def pairs_count(self) -> int:
        return sum(len(neighbors) for neighbors in self.graph.values())

# ============================================================================
# Binance API Integration
# ============================================================================

BINANCE_SPOT_API_URL = "https://api.binance.com/api/v3/ticker/price"
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")

async def fetch_binance_pairs() -> Dict[str, float]:
    if not BINANCE_API_KEY:
        raise HTTPException(status_code=500, detail="Binance API key not set in environment")
    
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(BINANCE_SPOT_API_URL, headers=headers)
        response.raise_for_status()
        try:
            resp_json = response.json()
        except Exception as json_err:
            raise HTTPException(status_code=500, detail=f"JSON parse error: {json_err}")
    
    pairs = {}
    for ticker in resp_json:
        symbol = ticker.get("symbol", "")
        price_str = ticker.get("price", "0")
        try:
            price = float(price_str)
            if price > 0:
                pairs[symbol] = price
        except ValueError:
            continue
    return pairs

def extract_coins_from_pair(pair: str) -> Tuple[Optional[str], Optional[str]]:
    suffixes = ["USDT", "USDC", "BUSD", "TUSD", "SUSD", "DAI", "USDE", "FDUSD",
                "USD", "USDK", "USDJ", "DOGE", "BTC", "ETH", "BNB", "SOL"]
    pair_upper = pair.upper()
    for suffix in suffixes:
        if pair_upper.endswith(suffix) and len(pair_upper) > len(suffix):
            return pair_upper[:-len(suffix)], suffix
    return None, None

def build_graph_from_pairs(pairs: Dict[str, float]) -> CoinGraph:
    graph = CoinGraph()
    for pair, price in pairs.items():
        coin1, coin2 = extract_coins_from_pair(pair)
        if coin1 and coin2:
            graph.add_pair(coin1, coin2, price)
    return graph

# ============================================================================
# DFS Arbitrage Path Finder
# ============================================================================

POPULAR_COINS = {"USDT", "USDC", "BTC", "ETH", "BNB", "SOL", "XRP", "TRX", "DOGE"}

def get_risk_level(coin: str) -> RiskLevel:
    return RiskLevel.SAFE if coin.upper() in POPULAR_COINS else RiskLevel.MEDIUM

def find_paths_dfs(graph: CoinGraph, start_coin: str, mode: ArbitrageMode,
                   max_depth: int = 4, max_paths: int = 500) -> List[List[str]]:
    start_coin = start_coin.upper()
    paths: List[List[str]] = []
    visited_set = set()

    def dfs(current: str, path: List[str], depth: int):
        if len(paths) >= max_paths:
            return
        if len(path) >= 3:
            end_coin = path[-1]
            is_valid = (
                (mode == ArbitrageMode.START_ONLY and end_coin == start_coin) or
                (mode == ArbitrageMode.POPULAR_END and end_coin.upper() in POPULAR_COINS) or
                (mode == ArbitrageMode.BOTH)
            )
            if is_valid:
                path_tuple = tuple(path)
                if path_tuple not in visited_set:
                    visited_set.add(path_tuple)
                    paths.append(path[:])
        if depth < max_depth:
            for edge in graph.get_neighbors(current):
                if len(path) <= 1 or edge.target != path[-2]:
                    path.append(edge.target)
                    dfs(edge.target, path, depth + 1)
                    path.pop()
    dfs(start_coin, [start_coin], 0)
    return paths

# ============================================================================
# Profit Calculation
# ============================================================================

TRADING_FEE = 0.001

def calculate_profit(graph: CoinGraph, path: List[str], start_amount: float) -> Tuple[float, float]:
    amount = start_amount
    for i in range(len(path) - 1):
        source = path[i].upper()
        target = path[i + 1].upper()
        edges = graph.get_neighbors(source)
        price = next((e.price for e in edges if e.target == target), None)
        if price is None:
            return 0, 0
        amount = amount * (1 - TRADING_FEE) * price
    profit_percent = ((amount - start_amount) / start_amount) * 100
    return amount, profit_percent

# ============================================================================
# FastAPI App
# ============================================================================

app = FastAPI(title="Crypto Arbitrage Backend", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_graph_cache: Optional[CoinGraph] = None
_graph_timestamp: Optional[str] = None

async def get_or_refresh_graph() -> Tuple[CoinGraph, str]:
    global _graph_cache, _graph_timestamp
    if _graph_cache is None:
        pairs = await fetch_binance_pairs()
        _graph_cache = build_graph_from_pairs(pairs)
        _graph_timestamp = datetime.utcnow().isoformat()
    return _graph_cache, _graph_timestamp

@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "crypto-arbitrage-backend"}

@app.get("/graph/info")
async def graph_info():
    graph, timestamp = await get_or_refresh_graph()
    return {
        "coins_count": graph.coins_count(),
        "pairs_count": graph.pairs_count(),
        "popular_coins": list(POPULAR_COINS),
        "fetch_timestamp": timestamp
    }

@app.post("/arbitrage/calculate", response_model=ArbitrageResponse)
async def calculate_arbitrage(request: ArbitrageRequest) -> ArbitrageResponse:
    graph, fetch_timestamp = await get_or_refresh_graph()
    start_coin_upper = request.start_coin.upper()
    if start_coin_upper not in graph.all_coins:
        raise HTTPException(status_code=400, detail=f"Coin '{request.start_coin}' not found")
    
    paths = find_paths_dfs(graph, start_coin_upper, request.mode, max_depth=4, max_paths=500)
    opportunities: List[PathOpportunity] = []
    for path in paths:
        end_amount, profit_percent = calculate_profit(graph, path, request.start_amount)
        if end_amount > 0:
            opportunities.append(PathOpportunity(
                path=path,
                start_amount=request.start_amount,
                end_amount=round(end_amount, 8),
                profit_percent=round(profit_percent, 6),
                end_coin=path[-1],
                risk=get_risk_level(path[-1])
            ))
    opportunities.sort(key=lambda x: x.profit_percent, reverse=True)
    return ArbitrageResponse(
        start_coin=start_coin_upper,
        start_amount=request.start_amount,
        mode=request.mode,
        opportunities=opportunities,
        total_count=len(opportunities),
        fetch_timestamp=fetch_timestamp
    )

@app.post("/arbitrage/refresh")
async def refresh_graph():
    global _graph_cache, _graph_timestamp
    pairs = await fetch_binance_pairs()
    _graph_cache = build_graph_from_pairs(pairs)
    _graph_timestamp = datetime.utcnow().isoformat()
    return {
        "status": "success",
        "message": "Graph refreshed",
        "coins_count": _graph_cache.coins_count(),
        "pairs_count": _graph_cache.pairs_count(),
        "fetch_timestamp": _graph_timestamp
    }

# ============================================================================
# Entry Point
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)
