"""
FastAPI Backend for Manual Crypto Arbitrage System (v0)
Fetches all spot pairs from Bybit, builds a coin graph, and finds arbitrage opportunities.
Run: uvicorn server:app --reload
"""

import asyncio
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
    """Mode for arbitrage path finding."""
    START_ONLY = "START_ONLY"
    POPULAR_END = "POPULAR_END"
    BOTH = "BOTH"


class RiskLevel(str, Enum):
    """Risk level based on coin popularity."""
    SAFE = "SAFE"
    MEDIUM = "MEDIUM"


class ArbitrageRequest(BaseModel):
    """Request model for arbitrage calculation."""
    start_coin: str = Field(..., min_length=1, max_length=20)
    start_amount: float = Field(..., gt=0)
    mode: ArbitrageMode

    @validator('start_coin')
    def validate_start_coin(cls, v):
        return v.upper()


class PathOpportunity(BaseModel):
    """Single arbitrage path opportunity."""
    path: List[str]
    start_amount: float
    end_amount: float
    profit_percent: float
    end_coin: str
    risk: RiskLevel


class ArbitrageResponse(BaseModel):
    """Response model for arbitrage opportunities."""
    start_coin: str
    start_amount: float
    mode: ArbitrageMode
    opportunities: List[PathOpportunity]
    total_count: int
    fetch_timestamp: str


# ============================================================================
# Coin Graph and Graph Utilities
# ============================================================================

@dataclass
class Edge:
    """Represents an edge in the coin graph (trading pair)."""
    target: str
    price: float


class CoinGraph:
    """
    Graph representation where:
    - Nodes are coins
    - Edges are spot pairs with prices
    - Both directions are included (A→B at price P, B→A at price 1/P)
    """

    def __init__(self):
        self.graph: Dict[str, List[Edge]] = defaultdict(list)
        self.all_coins: Set[str] = set()

    def add_edge(self, source: str, target: str, price: float):
        """Add directed edge from source to target with given price."""
        self.graph[source].append(Edge(target=target, price=price))
        self.all_coins.add(source)
        self.all_coins.add(target)

    def add_pair(self, coin1: str, coin2: str, price: float):
        """
        Add bidirectional pair (both directions).
        coin1 → coin2 at price P
        coin2 → coin1 at price 1/P
        """
        self.add_edge(coin1, coin2, price)
        if price > 0:
            self.add_edge(coin2, coin1, 1 / price)

    def get_neighbors(self, coin: str) -> List[Edge]:
        """Get all neighbors of a coin."""
        return self.graph.get(coin, [])

    def coins_count(self) -> int:
        """Return total number of coins in graph."""
        return len(self.all_coins)

    def pairs_count(self) -> int:
        """Return total number of edges in graph."""
        return sum(len(neighbors) for neighbors in self.graph.values())


# ============================================================================
# Bybit API Integration
# ============================================================================

BYBIT_SPOT_API_URL = "https://arbscap.netlify.app/api/fetch_bybit"


async def fetch_bybit_pairs() -> Dict[str, float]:
    """
    Fetch all spot pairs from Bybit with detailed logging.
    Returns dict: {pair_name: price} e.g., {"BTCUSDT": 45000.0}
    """
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            params = {
                "category": "spot",
                "limit": 1000,
            }
            
            all_tickers = []
            cursor = ""
            
            while True:
                if cursor:
                    params["cursor"] = cursor
                
                try:
                    response = await client.get(BYBIT_SPOT_API_URL, params=params)
                except httpx.RequestError as e:
                    # Network-level errors
                    print("Bybit request failed:", str(e))
                    raise HTTPException(
                        status_code=500,
                        detail=f"Bybit request failed: {str(e)}"
                    )

                # Log response details
                print("Bybit request URL:", response.url)
                print("Status code:", response.status_code)
                print("Response headers:", dict(response.headers))
                try:
                    resp_json = response.json()
                    print("Response body:", resp_json)
                except Exception:
                    resp_json = None
                    print("Response body is not JSON:", response.text)

                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as e:
                    raise HTTPException(
                        status_code=response.status_code,
                        detail=f"Bybit HTTP error: {str(e)}\nResponse body: {response.text}"
                    )

                if resp_json and resp_json.get("retCode") != 0:
                    raise HTTPException(
                        status_code=500,
                        detail=f"Bybit API error: {resp_json.get('retMsg', 'Unknown error')}"
                    )
                
                tickers = resp_json.get("result", {}).get("list", []) if resp_json else []
                if not tickers:
                    break
                
                all_tickers.extend(tickers)
                
                next_cursor = resp_json.get("result", {}).get("nextPageCursor", "") if resp_json else ""
                if not next_cursor:
                    break
                cursor = next_cursor
            
            pairs = {}
            for ticker in all_tickers:
                symbol = ticker.get("symbol", "")
                price_str = ticker.get("lastPrice", "0")
                
                try:
                    price = float(price_str)
                    if price > 0:
                        pairs[symbol] = price
                except ValueError:
                    continue
            
            print(f"Total pairs fetched: {len(pairs)}")
            return pairs
    
    except Exception as e:
        print("Unexpected error fetching Bybit pairs:", str(e))
        raise HTTPException(
            status_code=500,
            detail=f"Unexpected error fetching Bybit pairs: {str(e)}"
        )


def extract_coins_from_pair(pair: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Extract coin1 and coin2 from pair name.
    E.g., "BTCUSDT" → ("BTC", "USDT")
    Uses common stablecoin/fiat suffixes to determine split.
    """
    # Common stablecoin/quote currency suffixes (longest first)
    suffixes = ["USDT", "USDC", "BUSD", "TUSD", "SUSD", "DAI", "USDE", "FDUSD",
                "USD", "USDK", "USDJ", "DOGE", "BTC", "ETH", "BNB", "SOL"]
    
    pair_upper = pair.upper()
    
    for suffix in suffixes:
        if pair_upper.endswith(suffix) and len(pair_upper) > len(suffix):
            coin1 = pair_upper[:-len(suffix)]
            coin2 = suffix
            return coin1, coin2
    
    return None, None


def build_graph_from_pairs(pairs: Dict[str, float]) -> CoinGraph:
    """
    Build coin graph from Bybit pairs.
    Filter out pairs where coin extraction fails.
    """
    graph = CoinGraph()
    
    for pair, price in pairs.items():
        coin1, coin2 = extract_coins_from_pair(pair)
        
        if coin1 and coin2:
            graph.add_pair(coin1, coin2, price)
    
    return graph


# ============================================================================
# DFS Path Finding Algorithm
# ============================================================================

POPULAR_COINS = {"USDT", "USDC", "BTC", "ETH", "BNB", "SOL", "XRP", "TRX", "DOGE"}


def get_risk_level(coin: str) -> RiskLevel:
    """Determine risk level based on coin popularity."""
    return RiskLevel.SAFE if coin.upper() in POPULAR_COINS else RiskLevel.MEDIUM


def find_paths_dfs(
    graph: CoinGraph,
    start_coin: str,
    mode: ArbitrageMode,
    max_depth: int = 4,
    max_paths: int = 500
) -> List[List[str]]:
    """
    Find arbitrage paths using DFS.
    - max_depth: maximum path length (3-4 for triangular/quadrangular paths)
    - max_paths: limit number of paths to avoid timeout
    """
    start_coin = start_coin.upper()
    paths: List[List[str]] = []
    visited_set = set()

    def dfs(current: str, path: List[str], depth: int):
        """Recursively find paths using DFS."""
        if len(paths) >= max_paths:
            return

        # Check path validity based on mode
        if len(path) >= 3:
            end_coin = path[-1]
            
            is_valid = False
            if mode == ArbitrageMode.START_ONLY:
                is_valid = end_coin == start_coin
            elif mode == ArbitrageMode.POPULAR_END:
                is_valid = end_coin.upper() in POPULAR_COINS
            else:  # BOTH
                is_valid = True
            
            if is_valid:
                # Convert to tuple for hashing (avoid duplicate paths)
                path_tuple = tuple(path)
                if path_tuple not in visited_set:
                    visited_set.add(path_tuple)
                    paths.append(path[:])

        # Continue DFS if depth allows
        if depth < max_depth:
            for edge in graph.get_neighbors(current):
                # Allow revisiting coins but avoid immediate loops
                if len(path) <= 1 or edge.target != path[-2]:
                    path.append(edge.target)
                    dfs(edge.target, path, depth + 1)
                    path.pop()

    dfs(start_coin, [start_coin], 0)
    return paths


# ============================================================================
# Profit Calculation
# ============================================================================

TRADING_FEE = 0.001  # 0.1% per trade


def calculate_profit(
    graph: CoinGraph,
    path: List[str],
    start_amount: float
) -> Tuple[float, float]:
    """
    Calculate end amount and profit percentage for a path.
    Applies 0.1% trading fee at each leg.
    Returns (end_amount, profit_percent)
    """
    amount = start_amount
    
    for i in range(len(path) - 1):
        source = path[i].upper()
        target = path[i + 1].upper()
        
        # Find edge price
        edges = graph.get_neighbors(source)
        price = None
        for edge in edges:
            if edge.target == target:
                price = edge.price
                break
        
        if price is None:
            return 0, 0
        
        # Apply trading fee and price conversion
        amount = amount * (1 - TRADING_FEE) * price
    
    profit_percent = ((amount - start_amount) / start_amount) * 100
    return amount, profit_percent


# ============================================================================
# FastAPI Application
# ============================================================================

app = FastAPI(
    title="Crypto Arbitrage Backend",
    description="Manual crypto arbitrage system using Bybit spot pairs",
    version="1.0.0"
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global graph cache
_graph_cache: Optional[CoinGraph] = None
_graph_timestamp: Optional[str] = None


async def get_or_refresh_graph() -> Tuple[CoinGraph, str]:
    """Get cached graph or refresh from Bybit."""
    global _graph_cache, _graph_timestamp
    
    if _graph_cache is None:
        pairs = await fetch_bybit_pairs()
        _graph_cache = build_graph_from_pairs(pairs)
        _graph_timestamp = datetime.utcnow().isoformat()
    
    return _graph_cache, _graph_timestamp


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "service": "crypto-arbitrage-backend"}


@app.get("/graph/info")
async def graph_info():
    """Get information about the current coin graph."""
    graph, timestamp = await get_or_refresh_graph()
    return {
        "coins_count": graph.coins_count(),
        "pairs_count": graph.pairs_count(),
        "popular_coins": list(POPULAR_COINS),
        "fetch_timestamp": timestamp
    }


@app.post("/arbitrage/calculate", response_model=ArbitrageResponse)
async def calculate_arbitrage(request: ArbitrageRequest) -> ArbitrageResponse:
    """
    Calculate arbitrage opportunities.
    
    POST /arbitrage/calculate
    {
        "start_coin": "USDT",
        "start_amount": 1000,
        "mode": "START_ONLY|POPULAR_END|BOTH"
    }
    """
    try:
        # Get or refresh graph
        graph, fetch_timestamp = await get_or_refresh_graph()
        
        # Validate start coin exists
        start_coin_upper = request.start_coin.upper()
        if start_coin_upper not in graph.all_coins:
            raise HTTPException(
                status_code=400,
                detail=f"Coin '{request.start_coin}' not found in Bybit spot pairs"
            )
        
        # Find paths
        paths = find_paths_dfs(
            graph,
            request.start_coin,
            request.mode,
            max_depth=4,
            max_paths=500
        )
        
        # Calculate profits and create opportunities
        opportunities: List[PathOpportunity] = []
        
        for path in paths:
            end_amount, profit_percent = calculate_profit(
                graph,
                path,
                request.start_amount
            )
            
            # Only include profitable paths
            if end_amount > 0:
                opportunity = PathOpportunity(
                    path=path,
                    start_amount=request.start_amount,
                    end_amount=round(end_amount, 8),
                    profit_percent=round(profit_percent, 6),
                    end_coin=path[-1],
                    risk=get_risk_level(path[-1])
                )
                opportunities.append(opportunity)
        
        # Sort by profit percentage (highest first)
        opportunities.sort(key=lambda x: x.profit_percent, reverse=True)
        
        return ArbitrageResponse(
            start_coin=start_coin_upper,
            start_amount=request.start_amount,
            mode=request.mode,
            opportunities=opportunities,
            total_count=len(opportunities),
            fetch_timestamp=fetch_timestamp
        )
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error calculating arbitrage: {str(e)}"
        )


@app.post("/arbitrage/refresh")
async def refresh_graph():
    """Manually refresh the coin graph from Bybit."""
    global _graph_cache, _graph_timestamp
    
    try:
        pairs = await fetch_bybit_pairs()
        _graph_cache = build_graph_from_pairs(pairs)
        _graph_timestamp = datetime.utcnow().isoformat()
        
        return {
            "status": "success",
            "message": "Graph refreshed",
            "coins_count": _graph_cache.coins_count(),
            "pairs_count": _graph_cache.pairs_count(),
            "fetch_timestamp": _graph_timestamp
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to refresh graph: {str(e)}"
        )


# ============================================================================
# Entry Point
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        reload=True
                    )
    
