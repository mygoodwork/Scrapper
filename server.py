"""
FastAPI Backend for Real Binance Crypto Arbitrage System (v3)
Fetches all spot pairs from Binance and finds arbitrage opportunities using only actual Binance pairs.
Run: uvicorn server:app --reload
"""

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
# Data Models
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
# Coin Graph
# ============================================================================

@dataclass
class Edge:
    target: str
    price: float
    pair_symbol: str

class CoinGraph:
    def __init__(self):
        self.graph: Dict[str, List[Edge]] = defaultdict(list)
        self.all_coins: Set[str] = set()

    def add_edge(self, source: str, target: str, price: float, pair_symbol: str):
        self.graph[source].append(Edge(target=target, price=price, pair_symbol=pair_symbol))
        self.all_coins.add(source)
        self.all_coins.add(target)

    def add_pair(self, base: str, quote: str, price: float, symbol: str):
        self.add_edge(base, quote, price, symbol)
        if price > 0:
            self.add_edge(quote, base, 1 / price, symbol)

    def get_neighbors(self, coin: str) -> List[Edge]:
        return self.graph.get(coin, [])

# ============================================================================
# Binance API Integration
# ============================================================================

BINANCE_SPOT_API_URL = "https://api.binance.com/api/v3/ticker/price"

async def fetch_binance_pairs() -> Dict[str, float]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(BINANCE_SPOT_API_URL)
        response.raise_for_status()
        data = response.json()

    pairs = {}
    for ticker in data:
        symbol = ticker.get("symbol")
        price_str = ticker.get("price", "0")
        try:
            price = float(price_str)
            if price > 0:
                pairs[symbol] = price
        except ValueError:
            continue
    return pairs

def build_graph_from_pairs(pairs: Dict[str, float]) -> CoinGraph:
    graph = CoinGraph()
    # Only actual Binance pairs, detect base and quote
    QUOTE_COINS = ["USDT", "BUSD", "USDC", "BTC", "ETH", "BNB", "SOL", "DOGE", "TRX", "XRP"]
    for symbol, price in pairs.items():
        for quote in QUOTE_COINS:
            if symbol.endswith(quote) and len(symbol) > len(quote):
                base = symbol[:-len(quote)]
                graph.add_pair(base, quote, price, symbol)
                break
    return graph

# ============================================================================
# DFS Arbitrage Finder
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
                t = tuple(path)
                if t not in visited_set:
                    visited_set.add(t)
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

TRADING_FEE = 0.001  # 0.1%

def calculate_profit(graph: CoinGraph, path: List[str], start_amount: float) -> Tuple[float, float, List[str]]:
    amount = start_amount
    pairs_in_path = []
    for i in range(len(path) - 1):
        source = path[i].upper()
        target = path[i + 1].upper()
        edge = next((e for e in graph.get_neighbors(source) if e.target == target), None)
        if edge is None:
            return 0, 0, []  # skip invalid path
        amount *= (1 - TRADING_FEE) * edge.price
        pairs_in_path.append(edge.pair_symbol)
    profit_percent = ((amount - start_amount) / start_amount) * 100
    return amount, profit_percent, pairs_in_path

# ============================================================================
# FastAPI App
# ============================================================================

app = FastAPI(title="Crypto Arbitrage Backend v3", version="3.0.0")
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
    pairs = await fetch_binance_pairs()
    _graph_cache = build_graph_from_pairs(pairs)
    _graph_timestamp = datetime.utcnow().isoformat()
    return _graph_cache, _graph_timestamp

@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "crypto-arbitrage-backend-v3"}

@app.get("/graph/info")
async def graph_info():
    graph, timestamp = await get_or_refresh_graph()
    return {
        "coins_count": len(graph.all_coins),
        "pairs_count": sum(len(edges) for edges in graph.graph.values()),
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
        end_amount, profit_percent, pairs_in_path = calculate_profit(graph, path, request.start_amount)
        if profit_percent <= 0 or end_amount <= 0:
            continue
        opportunities.append(PathOpportunity(
            path=path,
            pairs=pairs_in_path,
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
    graph, timestamp = await get_or_refresh_graph()
    return {
        "status": "success",
        "message": "Graph refreshed",
        "coins_count": len(graph.all_coins),
        "pairs_count": sum(len(edges) for edges in graph.graph.values()),
        "fetch_timestamp": timestamp
    }

# ============================================================================
# Entry Point
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)
