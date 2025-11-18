"""
FastAPI Backend using real Binance orderbooks for arbitrage detection (final corrected)
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

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, validator

# ----------------------------
# Config
# ----------------------------
BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/price"
BINANCE_ORDERBOOK_URL = "https://api.binance.com/api/v3/depth"  # ?symbol=SYMBOL&limit=5
ORDERBOOK_LIMIT = 5
TRADING_FEE = float(os.getenv("TRADING_FEE", "0.001"))  # default 0.1% per trade
# Quote coin candidates (order matters: longer tokens first)
QUOTE_COINS = ["USDT", "BUSD", "USDC", "BTC", "ETH", "BNB", "SOL", "DOGE", "TRX", "XRP", "ADA"]

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
    pair_symbol: str  # e.g., "BTCUSDT"
    # price not stored here because we'll use orderbook bids/asks for live prices

class CoinGraph:
    def __init__(self):
        # adjacency: coin -> list of edges
        self.graph: Dict[str, List[Edge]] = defaultdict(list)
        # set of coins discovered
        self.all_coins: Set[str] = set()
        # mapping (base, quote) -> symbol string
        self.symbol_map: Dict[Tuple[str, str], str] = {}

    def add_pair(self, base: str, quote: str, symbol: str):
        base_u = base.upper()
        quote_u = quote.upper()
        if base_u == quote_u:
            return
        # forward: base -> quote  (selling base -> receive quote at bid)
        self.graph[base_u].append(Edge(target=quote_u, pair_symbol=symbol))
        # reverse: quote -> base (buying base with quote at ask)
        self.graph[quote_u].append(Edge(target=base_u, pair_symbol=symbol))
        self.all_coins.add(base_u)
        self.all_coins.add(quote_u)
        self.symbol_map[(base_u, quote_u)] = symbol
        self.symbol_map[(quote_u, base_u)] = symbol  # same symbol used for both directions

    def get_neighbors(self, coin: str) -> List[Edge]:
        return self.graph.get(coin.upper(), [])

# ----------------------------
# Binance helpers
# ----------------------------
async def fetch_all_binance_symbols() -> Dict[str, float]:
    """Fetch all ticker prices (we use this to discover available symbols)."""
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(BINANCE_TICKER_URL)
        r.raise_for_status()
        data = r.json()
    # data is list of {symbol, price}
    pairs: Dict[str, float] = {}
    for entry in data:
        sym = entry.get("symbol")
        price_str = entry.get("price", "0")
        try:
            price = float(price_str)
            if price > 0:
                pairs[sym] = price
        except Exception:
            continue
    return pairs

async def fetch_orderbook_best(symbol: str, client: httpx.AsyncClient) -> Optional[Tuple[float, float]]:
    """
    Fetch best (bid, ask) for a symbol from Binance orderbook.
    Returns (best_bid, best_ask) as floats.
    Returns None if symbol not found or error.
    """
    params = {"symbol": symbol, "limit": ORDERBOOK_LIMIT}
    try:
        r = await client.get(BINANCE_ORDERBOOK_URL, params=params)
        r.raise_for_status()
        data = r.json()
    except httpx.HTTPStatusError:
        return None
    except Exception:
        return None

    bids = data.get("bids", [])
    asks = data.get("asks", [])
    if not bids or not asks:
        return None
    try:
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        return best_bid, best_ask
    except Exception:
        return None

# ----------------------------
# Build graph only from real Binance symbols
# ----------------------------
def build_graph_from_symbols(symbols: Dict[str, float]) -> CoinGraph:
    graph = CoinGraph()
    # iterate all returned symbols, attempt to split into base/quote using QUOTE_COINS
    for sym in symbols.keys():
        sym_u = sym.upper()
        matched = False
        for q in QUOTE_COINS:
            if sym_u.endswith(q) and len(sym_u) > len(q):
                base = sym_u[: len(sym_u) - len(q)]
                quote = q
                # ignore if base empty or equals quote
                if base and base != quote:
                    graph.add_pair(base, quote, sym_u)
                    matched = True
                break
        # if not matched, skip (exotic or unknown quoting)
    return graph

# ----------------------------
# Path finding (DFS)
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
            else:  # BOTH
                ok = True
            if ok:
                t = tuple(path)
                if t not in seen:
                    seen.add(t)
                    paths.append(path.copy())
        if depth < max_depth:
            for edge in graph.get_neighbors(current):
                # avoid immediate back-and-forth
                if len(path) <= 1 or edge.target != path[-2]:
                    path.append(edge.target)
                    dfs(edge.target, path, depth + 1)
                    path.pop()

    dfs(start, [start], 0)
    return paths

# ----------------------------
# Profit simulation using real orderbooks
# ----------------------------
async def simulate_path_profit(
    graph: CoinGraph,
    path: List[str],
    start_amount: float,
    orderbook_cache: Dict[str, Tuple[float, float]],
    client: httpx.AsyncClient,
    trading_fee: float = TRADING_FEE
) -> Tuple[float, float, List[str]]:
    """
    Simulate trades along path using best bid/ask per symbol.
    - orderbook_cache: symbol -> (bid, ask)
    Returns (end_amount, profit_percent, pairs_used) or (0,0,[]) if any missing.
    """
    amount = float(start_amount)
    pairs_used: List[str] = []

    # For each leg from source -> target
    for i in range(len(path) - 1):
        source = path[i].upper()
        target = path[i + 1].upper()
        # find symbol for (source,target)
        symbol = graph.symbol_map.get((source, target))
        if not symbol:
            # No direct symbol: path invalid
            return 0.0, 0.0, []

        # fetch orderbook best prices (cached)
        if symbol not in orderbook_cache:
            ob = await fetch_orderbook_best(symbol, client)
            if not ob:
                return 0.0, 0.0, []
            orderbook_cache[symbol] = ob
        best_bid, best_ask = orderbook_cache[symbol]  # floats

        # Determine conversion type:
        # If symbol is BASE+QUOTE (e.g., BTCUSDT) and we are converting QUOTE -> BASE (USDT->BTC),
        # we must BUY BASE at the ASK: base_amount = quote_amount / ask
        # If converting BASE -> QUOTE (BTC->USDT), we SELL BASE at the BID: quote_amount = base_amount * bid
        # Because graph.symbol_map has same symbol for both (source,target) directions, we need to know base/quote:
        # derive base/quote from symbol using QUOTE_COINS
        sym_u = symbol.upper()
        base = None
        quote = None
        for q in QUOTE_COINS:
            if sym_u.endswith(q) and len(sym_u) > len(q):
                base = sym_u[: len(sym_u) - len(q)]
                quote = q
                break
        if base is None or quote is None:
            return 0.0, 0.0, []

        # Case 1: source == quote and target == base => buy base using quote (use ask)
        if source == quote and target == base:
            ask = best_ask
            if ask <= 0:
                return 0.0, 0.0, []
            # amount (in quote) -> base
            # apply fee on the notional? commonly fee applied on traded amount; we'll subtract fee on result:
            base_amount = amount / ask
            # apply fee (we assume fee applied on each trade value; for simplicity we multiply by (1 - fee))
            base_amount = base_amount * (1 - trading_fee)
            amount = base_amount  # now in base units
        # Case 2: source == base and target == quote => sell base to get quote (use bid)
        elif source == base and target == quote:
            bid = best_bid
            if bid <= 0:
                return 0.0, 0.0, []
            quote_amount = amount * bid
            quote_amount = quote_amount * (1 - trading_fee)
            amount = quote_amount
        else:
            # Not matching expected base/quote arrangement; path invalid
            return 0.0, 0.0, []

        pairs_used.append(symbol)

    profit_percent = ((amount - start_amount) / start_amount) * 100 if start_amount > 0 else 0.0
    return amount, profit_percent, pairs_used

# ----------------------------
# FastAPI app
# ----------------------------
app = FastAPI(title="Binance Orderbook Arbitrage", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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

@app.post("/arbitrage/calculate", response_model=ArbitrageResponse)
async def calculate_arbitrage(request: ArbitrageRequest, limit: int = Query(500, ge=1, le=5000)):
    """
    Calculate arbitrage opportunities.
    - Uses live orderbook best bid/ask for price simulation.
    - `limit` sets max candidate paths to evaluate (safety).
    """
    graph, fetch_timestamp = await get_or_refresh_graph()
    start_coin = request.start_coin.upper()
    if start_coin not in graph.all_coins:
        raise HTTPException(status_code=400, detail=f"Start coin '{start_coin}' not available in Binance pairs")

    candidate_paths = find_paths_dfs(graph, start_coin, request.mode, max_depth=4, max_paths=limit)
    opportunities: List[PathOpportunity] = []

    # We'll reuse a single HTTP client and an orderbook cache for this request
    orderbook_cache: Dict[str, Tuple[float, float]] = {}
    async with httpx.AsyncClient(timeout=15.0) as client:
        # Evaluate paths (sequentially). Could be parallelized but beware rate limiting.
        for path in candidate_paths:
            end_amount, profit_percent, pairs_used = await simulate_path_profit(
                graph, path, request.start_amount, orderbook_cache, client, trading_fee=TRADING_FEE
            )
            # include only profitable paths
            if profit_percent > 0 and end_amount > 0 and pairs_used:
                opportunities.append(PathOpportunity(
                    path=path,
                    pairs=pairs_used,
                    start_amount=request.start_amount,
                    end_amount=round(end_amount, 12),
                    profit_percent=round(profit_percent, 8),
                    end_coin=path[-1],
                    risk=get_risk_level(path[-1])
                ))

    # sort by profit desc
    opportunities.sort(key=lambda x: x.profit_percent, reverse=True)
    return ArbitrageResponse(
        start_coin=start_coin,
        start_amount=request.start_amount,
        mode=request.mode,
        opportunities=opportunities,
        total_count=len(opportunities),
        fetch_timestamp=fetch_timestamp,
    )

# ----------------------------
# Run
# ----------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=True)
