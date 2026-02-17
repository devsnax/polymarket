"""
core/feed.py — Real-time data engine.

Subscribes to three Binance WebSocket streams simultaneously:
  1. Trade stream  → every individual trade (aggressor buy/sell)
  2. Book ticker   → best bid/ask updated on every change
  3. Depth stream  → top 20 levels of orderbook

Also polls REST for funding rate + perp basis every 30s.

Everything is stored in shared state (State class) that the
signal engine reads from.
"""

import json
import time
import threading
import logging
import collections
from dataclasses import dataclass, field
from typing import Optional

import requests
import websocket   # pip install websocket-client

import config

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
#  SHARED STATE  (thread-safe reads via lock)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Trade:
    price:      float
    qty:        float
    is_buyer:   bool   # True = buyer was aggressor (market buy)
    timestamp:  float  # unix seconds

@dataclass
class OrderBookLevel:
    price: float
    qty:   float

@dataclass
class State:
    """
    Everything the signal engine needs, updated in real time.
    Read with state.lock held for consistency.
    """
    lock:        threading.Lock = field(default_factory=threading.Lock)

    # ── Trade tape (last 10 minutes of trades) ────────────────────────────
    trades:      collections.deque = field(
                     default_factory=lambda: collections.deque(maxlen=6000))

    # ── Order book ────────────────────────────────────────────────────────
    best_bid:    float = 0.0
    best_ask:    float = 0.0
    bids:        list  = field(default_factory=list)   # [(price, qty), ...]
    asks:        list  = field(default_factory=list)

    # ── Derived metrics (updated each tick) ───────────────────────────────
    # CVD per window: {1: float, 3: float, 5: float}
    cvd:         dict  = field(default_factory=dict)

    # OB imbalance per depth: {5: float, 10: float, 20: float}
    ob_imbalance: dict = field(default_factory=dict)

    # ── Perp / funding (from REST poll) ──────────────────────────────────
    funding_rate:  float = 0.0   # current funding rate
    funding_delta: float = 0.0   # change since last poll
    perp_price:    float = 0.0   # BTC perpetual price
    spot_price:    float = 0.0   # BTC spot price
    perp_basis:    float = 0.0   # (perp - spot) / spot

    # ── Connection health ─────────────────────────────────────────────────
    ws_connected:  bool  = False
    last_trade_ts: float = 0.0

    def mid_price(self) -> float:
        if self.best_bid and self.best_ask:
            return (self.best_bid + self.best_ask) / 2
        return self.spot_price


# ═══════════════════════════════════════════════════════════════════════════
#  WEBSOCKET FEED
# ═══════════════════════════════════════════════════════════════════════════

class BinanceFeed:
    """
    Connects to Binance combined WebSocket stream.
    Runs in a background thread — call .start() and forget it.
    """

    def __init__(self, state: State):
        self.state  = state
        self._ws    = None
        self._thread = None
        self._stop  = threading.Event()

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name="BinanceFeed")
        self._thread.start()
        logger.info("BinanceFeed started")

    def stop(self):
        self._stop.set()
        if self._ws:
            self._ws.close()

    def _build_url(self) -> str:
        sym = config.SYMBOL
        streams = [
            f"{sym}@aggTrade",          # individual trades
            f"{sym}@bookTicker",        # best bid/ask
            f"{sym}@depth20@100ms",     # top 20 OB levels
        ]
        return f"{config.BINANCE_WS_URL}?streams={'/'.join(streams)}"

    def _run(self):
        while not self._stop.is_set():
            try:
                url = self._build_url()
                logger.info("Connecting to %s", url[:60] + "...")
                self._ws = websocket.WebSocketApp(
                    url,
                    on_open    = self._on_open,
                    on_message = self._on_message,
                    on_error   = self._on_error,
                    on_close   = self._on_close,
                )
                self._ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                logger.error("WebSocket error: %s", e)
            if not self._stop.is_set():
                logger.info("Reconnecting in 3s...")
                time.sleep(3)

    def _on_open(self, ws):
        with self.state.lock:
            self.state.ws_connected = True
        logger.info("WebSocket connected ✓")

    def _on_close(self, ws, code, msg):
        with self.state.lock:
            self.state.ws_connected = False
        logger.warning("WebSocket closed: %s %s", code, msg)

    def _on_error(self, ws, error):
        logger.error("WebSocket error: %s", error)

    def _on_message(self, ws, raw: str):
        try:
            msg    = json.loads(raw)
            stream = msg.get("stream", "")
            data   = msg.get("data", {})

            if "aggTrade" in stream:
                self._handle_trade(data)
            elif "bookTicker" in stream:
                self._handle_ticker(data)
            elif "depth" in stream:
                self._handle_depth(data)
        except Exception as e:
            logger.debug("Message parse error: %s", e)

    def _handle_trade(self, d: dict):
        """
        aggTrade message:
          p = price, q = qty, m = true if seller was maker
          (m=True means the buyer was aggressor → market buy)
        """
        trade = Trade(
            price    = float(d["p"]),
            qty      = float(d["q"]),
            is_buyer = not bool(d["m"]),   # m=True → seller maker → buyer aggressor
            timestamp= time.time(),
        )
        with self.state.lock:
            self.state.trades.append(trade)
            self.state.spot_price  = trade.price
            self.state.last_trade_ts = trade.timestamp

    def _handle_ticker(self, d: dict):
        with self.state.lock:
            self.state.best_bid = float(d["b"])
            self.state.best_ask = float(d["a"])

    def _handle_depth(self, d: dict):
        bids = [(float(p), float(q)) for p, q in d.get("bids", [])]
        asks = [(float(p), float(q)) for p, q in d.get("asks", [])]
        with self.state.lock:
            self.state.bids = sorted(bids, key=lambda x: -x[0])  # highest first
            self.state.asks = sorted(asks, key=lambda x:  x[0])  # lowest first


# ═══════════════════════════════════════════════════════════════════════════
#  REST POLLER  (funding rate + perp price)
# ═══════════════════════════════════════════════════════════════════════════

class RestPoller:
    """
    Polls Binance REST every 30 seconds for:
      - Perpetual funding rate
      - Perpetual price (for basis calculation)
    """

    def __init__(self, state: State):
        self.state   = state
        self._thread = None
        self._stop   = threading.Event()
        self._last_funding = 0.0

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name="RestPoller")
        self._thread.start()
        logger.info("RestPoller started")

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                self._fetch_funding()
            except Exception as e:
                logger.warning("REST poll failed: %s", e)
            self._stop.wait(config.FUNDING_INTERVAL)

    def _fetch_funding(self):
        # Funding rate
        resp = requests.get(
            f"{config.BINANCE_REST_URL}/fapi/v1/premiumIndex",
            params={"symbol": config.SYMBOL_UP},
            timeout=8,
        )
        if resp.ok:
            d = resp.json()
            new_funding  = float(d.get("lastFundingRate", 0))
            perp_price   = float(d.get("markPrice", 0))

            with self.state.lock:
                self.state.funding_delta = new_funding - self.state.funding_rate
                self.state.funding_rate  = new_funding
                self.state.perp_price    = perp_price
                spot = self.state.spot_price
                if spot > 0:
                    self.state.perp_basis = (perp_price - spot) / spot

            logger.debug("Funding: %.6f  Perp: %.2f  Basis: %.6f",
                         new_funding, perp_price,
                         (perp_price - self.state.spot_price) / (self.state.spot_price or 1))


# ═══════════════════════════════════════════════════════════════════════════
#  METRIC COMPUTER  (runs every tick, derived from raw state)
# ═══════════════════════════════════════════════════════════════════════════

class MetricComputer:
    """
    Called every tick to compute derived metrics from raw trade + OB data.
    Updates state in place.
    """

    def __init__(self, state: State):
        self.state = state

    def compute(self):
        with self.state.lock:
            self._compute_cvd()
            self._compute_ob_imbalance()

    def _compute_cvd(self):
        """
        CVD = sum of (buy_qty - sell_qty) over window.
        Positive = net buying pressure. Negative = net selling.
        """
        now    = time.time()
        trades = list(self.state.trades)

        for minutes in config.CVD_WINDOWS:
            cutoff = now - (minutes * 60)
            window = [t for t in trades if t.timestamp >= cutoff]
            if not window:
                self.state.cvd[minutes] = 0.0
                continue
            buy_vol  = sum(t.qty for t in window if t.is_buyer)
            sell_vol = sum(t.qty for t in window if not t.is_buyer)
            total    = buy_vol + sell_vol
            # Normalise to [-1, +1]
            self.state.cvd[minutes] = (buy_vol - sell_vol) / (total + 1e-9)

    def _compute_ob_imbalance(self):
        """
        OB imbalance at each depth level.
        +1 = all bids, -1 = all asks, 0 = balanced.
        """
        bids = self.state.bids
        asks = self.state.asks

        for n in config.OB_DEPTH_LEVELS:
            bid_qty = sum(q for _, q in bids[:n])
            ask_qty = sum(q for _, q in asks[:n])
            total   = bid_qty + ask_qty
            if total < 1e-9:
                self.state.ob_imbalance[n] = 0.0
            else:
                self.state.ob_imbalance[n] = (bid_qty - ask_qty) / total


# ═══════════════════════════════════════════════════════════════════════════
#  POLYMARKET DATA
# ═══════════════════════════════════════════════════════════════════════════

class PolymarketFeed:
    """
    Fetches active BTC 5-min markets and their implied probabilities.
    Cached for 30 seconds to avoid hammering the API.
    """

    def __init__(self):
        self._cache     = []
        self._cache_ts  = 0.0
        self._cache_ttl = 30   # seconds

    def get_btc_markets(self) -> list[dict]:
        now = time.time()
        if self._cache and (now - self._cache_ts) < self._cache_ttl:
            return self._cache

        try:
            resp = requests.get(
                f"{config.GAMMA_URL}/markets",
                params={"active": "true", "closed": "false", "limit": 300},
                timeout=10,
            )
            resp.raise_for_status()
            all_markets = resp.json()

            btc = [
                m for m in all_markets
                if isinstance(m, dict)
                and "bitcoin" in m.get("question", "").lower()
                and "up or down" in m.get("question", "").lower()
            ]
            self._cache    = btc
            self._cache_ts = now
            logger.info("Found %d active BTC markets", len(btc))
            return btc
        except Exception as e:
            logger.warning("Polymarket fetch failed: %s", e)
            return self._cache

    def get_implied_prob(self, market: dict) -> float:
        """
        Returns the market-implied probability that BTC goes UP.
        YES token price = implied probability of Up outcome.
        """
        try:
            prices = json.loads(market.get("outcomePrices", "[0.5, 0.5]"))
            outcomes = json.loads(market.get("outcomes", '["Up", "Down"]'))
            for outcome, price in zip(outcomes, prices):
                if "up" in str(outcome).lower() or "higher" in str(outcome).lower():
                    return float(price)
            return float(prices[0])
        except:
            return 0.5

    def get_token_ids(self, market: dict) -> tuple[str, str]:
        """Returns (yes_token_id, no_token_id)."""
        try:
            ids = json.loads(market.get("clobTokenIds", "[]"))
            if len(ids) >= 2:
                return ids[0], ids[1]
        except:
            pass
        return "", ""