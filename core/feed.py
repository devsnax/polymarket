"""
core/feed.py — Real-time data engine.

Subscribes to Coinbase WebSocket (no geo-restrictions):
  1. Matches channel  → every individual trade (aggressor buy/sell)
  2. Level2 channel   → full orderbook snapshots + updates

Also polls REST for 24h stats (volume, price change).

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

    # ── 24h stats (from REST poll) ────────────────────────────────────────
    funding_rate:  float = 0.0   # Not available on Coinbase spot — always 0
    funding_delta: float = 0.0   # Not available
    perp_price:    float = 0.0   # Not available (no perp on Coinbase spot)
    spot_price:    float = 0.0   # BTC spot price
    perp_basis:    float = 0.0   # Not available

    # ── Connection health ─────────────────────────────────────────────────
    ws_connected:  bool  = False
    last_trade_ts: float = 0.0

    def mid_price(self) -> float:
        if self.best_bid and self.best_ask:
            return (self.best_bid + self.best_ask) / 2
        return self.spot_price


# ═══════════════════════════════════════════════════════════════════════════
#  WEBSOCKET FEED (Coinbase)
# ═══════════════════════════════════════════════════════════════════════════

class CoinbaseFeed:
    """
    Connects to Coinbase WebSocket.
    Subscribes to matches (trades) and level2 (orderbook).
    Runs in a background thread — call .start() and forget it.
    """

    def __init__(self, state: State):
        self.state  = state
        self._ws    = None
        self._thread = None
        self._stop  = threading.Event()

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name="CoinbaseFeed")
        self._thread.start()
        logger.info("CoinbaseFeed started")

    def stop(self):
        self._stop.set()
        if self._ws:
            self._ws.close()

    def _run(self):
        while not self._stop.is_set():
            try:
                logger.info("Connecting to Coinbase WebSocket...")
                self._ws = websocket.WebSocketApp(
                    config.COINBASE_WS_URL,
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
        # Subscribe to matches (trades) and level2 (orderbook)
        subscribe = {
            "type": "subscribe",
            "product_ids": [config.SYMBOL],
            "channels": ["matches", "level2"]
        }
        ws.send(json.dumps(subscribe))
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
            msg = json.loads(raw)
            msg_type = msg.get("type")

            if msg_type == "match" or msg_type == "last_match":
                self._handle_trade(msg)
            elif msg_type == "snapshot":
                self._handle_snapshot(msg)
            elif msg_type == "l2update":
                self._handle_l2update(msg)
        except Exception as e:
            logger.debug("Message parse error: %s", e)

    def _handle_trade(self, d: dict):
        """
        match message:
          price, size, side (buy/sell from taker perspective)
          side='buy' means taker bought (aggressor buy)
        """
        try:
            trade = Trade(
                price    = float(d["price"]),
                qty      = float(d["size"]),
                is_buyer = d["side"] == "buy",
                timestamp= time.time(),
            )
            with self.state.lock:
                self.state.trades.append(trade)
                self.state.spot_price  = trade.price
                self.state.last_trade_ts = trade.timestamp
        except:
            pass

    def _handle_snapshot(self, d: dict):
        """Initial orderbook snapshot"""
        try:
            bids = [(float(p), float(q)) for p, q in d.get("bids", [])]
            asks = [(float(p), float(q)) for p, q in d.get("asks", [])]
            with self.state.lock:
                self.state.bids = sorted(bids, key=lambda x: -x[0])[:20]
                self.state.asks = sorted(asks, key=lambda x:  x[0])[:20]
                if self.state.bids:
                    self.state.best_bid = self.state.bids[0][0]
                if self.state.asks:
                    self.state.best_ask = self.state.asks[0][0]
        except:
            pass

    def _handle_l2update(self, d: dict):
        """Incremental orderbook updates"""
        try:
            changes = d.get("changes", [])
            with self.state.lock:
                for side, price_str, size_str in changes:
                    price = float(price_str)
                    size  = float(size_str)
                    
                    if side == "buy":
                        # Update bids
                        if size == 0:
                            self.state.bids = [(p, q) for p, q in self.state.bids if p != price]
                        else:
                            # Remove old, insert new
                            self.state.bids = [(p, q) for p, q in self.state.bids if p != price]
                            self.state.bids.append((price, size))
                            self.state.bids = sorted(self.state.bids, key=lambda x: -x[0])[:20]
                    else:  # sell
                        if size == 0:
                            self.state.asks = [(p, q) for p, q in self.state.asks if p != price]
                        else:
                            self.state.asks = [(p, q) for p, q in self.state.asks if p != price]
                            self.state.asks.append((price, size))
                            self.state.asks = sorted(self.state.asks, key=lambda x: x[0])[:20]
                
                if self.state.bids:
                    self.state.best_bid = self.state.bids[0][0]
                if self.state.asks:
                    self.state.best_ask = self.state.asks[0][0]
        except:
            pass


# Alias for backward compatibility
BinanceFeed = CoinbaseFeed


# ═══════════════════════════════════════════════════════════════════════════
#  REST POLLER  (24h stats from Coinbase)
# ═══════════════════════════════════════════════════════════════════════════

class RestPoller:
    """
    Polls Coinbase REST every 60 seconds for 24h stats.
    NOTE: Coinbase spot doesn't have funding rate or perp basis.
    Those signals will always be 0 / neutral.
    """

    def __init__(self, state: State):
        self.state   = state
        self._thread = None
        self._stop   = threading.Event()

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name="RestPoller")
        self._thread.start()
        logger.info("RestPoller started")

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                self._fetch_stats()
            except Exception as e:
                logger.warning("REST poll failed: %s", e)
            self._stop.wait(60)  # Poll less often since no funding to track

    def _fetch_stats(self):
        # Get 24h stats
        try:
            resp = requests.get(
                f"{config.COINBASE_REST_URL}/products/{config.SYMBOL}/stats",
                timeout=8,
            )
            if resp.ok:
                d = resp.json()
                # Just log it for now
                logger.debug("24h stats: volume=%s", d.get("volume"))
        except:
            pass


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
    Cached for 10 seconds (markets refresh frequently).
    """

    def __init__(self):
        self._cache     = []
        self._cache_ts  = 0.0
        self._cache_ttl = 10   # seconds (more frequent checks)
        self._last_market_count = 0

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
                and ("up or down" in m.get("question", "").lower()
                     or "higher or lower" in m.get("question", "").lower()
                     or "btc" in m.get("question", "").lower())
            ]
            
            # Log market count changes (helps debug market windows)
            if len(btc) != self._last_market_count:
                if len(btc) > self._last_market_count:
                    logger.info("📈 New BTC markets appeared: %d active", len(btc))
                elif len(btc) < self._last_market_count:
                    logger.info("📉 Markets closed: %d remaining", len(btc))
                self._last_market_count = len(btc)
            
            self._cache    = btc
            self._cache_ts = now
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