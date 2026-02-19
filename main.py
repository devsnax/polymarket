"""
main.py — Start the bot.

Run with:
  python main.py

Then open http://localhost:5000 in your browser.

NEW ARCHITECTURE (synced to Polymarket):
  1. CoinbaseFeed starts — streams live BTC price + trades
  2. RestPoller starts — fetches funding rate
  3. Main loop WAITS for new Polymarket markets to open
  4. When new market detected:
     a. Capture entry price at market open
     b. Compute all signals based on last 5 minutes of data
     c. Generate ONE prediction for this specific market
     d. If edge > threshold → open paper position
     e. Wait exactly 5 minutes
     f. Check outcome (price vs entry price)
     g. Update W/L for all signals → self-calibrate
     h. Repeat for next market
  5. Dashboard serves http://localhost:5000
"""

import time
import logging
import sys
import os
from datetime import datetime, timezone

# Make sure all submodules are importable
sys.path.insert(0, os.path.dirname(__file__))

import config
from core.feed      import State, CoinbaseFeed, RestPoller, MetricComputer, PolymarketFeed
from signals.engine import SignalEngine
from core.logger    import BotLogger
from core.positions import PositionTracker
from web.dashboard  import set_bot, run_dashboard

# ── Logging setup ─────────────────────────────────────────────────────────
os.makedirs(config.LOG_DIR, exist_ok=True)

logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt = "%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(f"{config.LOG_DIR}/bot.log"),
    ]
)
logger = logging.getLogger("main")


# ═══════════════════════════════════════════════════════════════════════════
#  BOT — POLYMARKET-SYNCED ARCHITECTURE
# ═══════════════════════════════════════════════════════════════════════════

class Bot:

    def __init__(self):
        logger.info("=" * 60)
        logger.info("  POLYMARKET BTC SIGNAL BOT")
        logger.info("  Synced to Polymarket 5-minute market cycles")
        logger.info("  Paper trading mode — signals only")
        logger.info("=" * 60)

        # Shared state (lives in RAM, updated by WebSocket)
        self.state       = State()

        # Data feeds
        self.feed        = CoinbaseFeed(self.state)
        self.poller      = RestPoller(self.state)
        self.metrics     = MetricComputer(self.state)
        self.poly        = PolymarketFeed()

        # Signal + position engine
        self.engine      = SignalEngine()
        self.bot_logger  = BotLogger()
        self.positions   = PositionTracker(self.bot_logger, self.engine)

        # Last prediction (exposed to dashboard)
        self.last_prediction = None

        # Track which markets we've already processed
        self._seen_markets = set()

    def start(self):
        # Start data feeds
        self.feed.start()
        self.poller.start()

        # Start dashboard
        set_bot(self)
        run_dashboard(config.DASHBOARD_HOST, config.DASHBOARD_PORT)

        # Wait for WebSocket to connect and build up some trade history
        logger.info("Waiting 15s for WebSocket to accumulate data...")
        time.sleep(15)

        # Main loop — watches for NEW Polymarket markets
        logger.info("Watching Polymarket for new 5-minute markets...")
        logger.info("Bot will fire ONE signal per market at market open.")
        
        while True:
            try:
                self._watch_for_new_market()
            except KeyboardInterrupt:
                logger.info("Shutdown requested.")
                self._shutdown()
                break
            except Exception as e:
                logger.error("Loop error: %s", e, exc_info=True)
            time.sleep(5)  # Check for new markets every 5 seconds

    def _watch_for_new_market(self):
        """
        Check if a NEW Polymarket 5-min market has appeared.
        If yes → process it immediately.
        """
        # 1. Check WebSocket health
        with self.state.lock:
            ws_connected = self.state.ws_connected
            spot_price   = self.state.spot_price

        if not ws_connected:
            logger.warning("WebSocket disconnected — waiting for reconnection...")
            return

        if spot_price == 0:
            logger.debug("No price data yet")
            return

        # 2. Fetch current Polymarket markets
        markets = self.poly.get_btc_markets()
        if not markets:
            # No markets active — this is normal between market windows
            self._update_prediction_no_market()
            return

        # 3. Find the most recent market (earliest end time = just opened)
        market = self._get_newest_market(markets)
        if not market:
            return

        market_id = market.get("id", "")
        
        # 4. Have we already processed this market?
        if market_id in self._seen_markets:
            # Already processed — just update dashboard with current data
            self._update_prediction_for_open_market(market)
            # Check if any positions need resolution
            self._check_resolutions()
            return

        # 5. NEW MARKET DETECTED → Process it
        logger.info("=" * 60)
        logger.info("🆕 NEW MARKET OPENED: %s", market.get("question", "")[:60])
        logger.info("=" * 60)
        
        self._process_new_market(market)
        self._seen_markets.add(market_id)

        # Clean up old seen markets (keep last 50)
        if len(self._seen_markets) > 50:
            self._seen_markets = set(list(self._seen_markets)[-50:])

    def _get_newest_market(self, markets: list) -> dict:
        """
        Returns the market that opened most recently.
        Polymarket markets have an 'endDate' timestamp.
        Newest market = earliest endDate (just opened).
        """
        if not markets:
            return None
        
        # Sort by endDate ascending (earliest = newest market)
        try:
            sorted_markets = sorted(
                markets,
                key=lambda m: m.get("endDate", "9999-99-99")
            )
            return sorted_markets[0]
        except:
            return markets[0]

    def _process_new_market(self, market: dict):
        """
        A new 5-minute market just opened.
        
        Steps:
        1. Capture entry price RIGHT NOW
        2. Compute all signals based on data from last few minutes
        3. Generate ONE prediction: Up or Down
        4. Open paper position (always, no edge filtering)
        5. Wait 5 minutes
        6. Check outcome → update W/L
        """
        market_id       = market.get("id", "")
        market_question = market.get("question", "BTC Up or Down")
        market_end      = market.get("endDate", "")
        
        # Get market implied probability
        market_implied = self.poly.get_implied_prob(market)
        token_yes, token_no = self.poly.get_token_ids(market)

        # Capture entry price at market open
        with self.state.lock:
            entry_price = self.state.spot_price

        logger.info("🆕 NEW MARKET: %s", market_question[:70])
        logger.info("💰 Entry: $%.2f | Market pricing Up at: %.1f%%", 
                   entry_price, market_implied * 100)

        # Compute all derived metrics from accumulated data
        self.metrics.compute()

        # Run signal engine
        prediction = self.engine.run(
            state           = self.state,
            market_implied  = market_implied,
            market_id       = market_id,
            market_question = market_question,
            token_yes       = token_yes,
            token_no        = token_no,
        )
        self.last_prediction = prediction

        # Log to CSV
        self.bot_logger.log_signal(prediction)

        # Decide direction: if model says >50% Up → bet Up, else bet Down
        if prediction.prob_up >= 0.50:
            direction = "Up"
            confidence = prediction.prob_up
        else:
            direction = "Down"
            confidence = 1 - prediction.prob_up

        # Console output
        logger.info("🎯 SIGNAL: %s | Model: %.1f%% | Confidence: %.1f%%",
                   direction, prediction.prob_up * 100, confidence * 100)

        # ALWAYS open position (no edge filtering)
        pos = self.positions.open_position(
            market_id      = market_id,
            question       = market_question,
            direction      = direction,
            confidence     = confidence,
            edge           = 0,  # not using edge anymore
            entry_price    = entry_price,
            token_yes      = token_yes,
            token_no       = token_no,
            prediction     = prediction,
        )
        
        if pos:
            logger.info("✅ Position: %s | Bet: $%.2f", direction, config.PAPER_BET_USD)
        else:
            logger.warning("⚠️  Failed to open position (duplicate market?)")

        logger.info("⏱️  Market closes in ~5 minutes...")
        logger.info("")

    def _update_prediction_for_open_market(self, market: dict):
        """
        Update the dashboard prediction for a market we're already tracking.
        (Called every 5 seconds while waiting for resolution)
        """
        market_implied = self.poly.get_implied_prob(market)
        self.metrics.compute()
        
        prediction = self.engine.run(
            state           = self.state,
            market_implied  = market_implied,
            market_id       = market.get("id", ""),
            market_question = market.get("question", ""),
        )
        self.last_prediction = prediction

    def _update_prediction_no_market(self):
        """Keep dashboard updated even when no markets are active."""
        self.metrics.compute()
        prediction = self.engine.run(
            state          = self.state,
            market_implied = 0.5,
        )
        self.last_prediction = prediction

    def _check_resolutions(self):
        """Check if any open positions should be resolved."""
        with self.state.lock:
            current_price = self.state.spot_price
        
        self.positions.check_resolutions(current_price)

    def _shutdown(self):
        logger.info("Shutting down...")
        self.feed.stop()
        self.poller.stop()
        logger.info("")
        logger.info("=" * 60)
        logger.info("  FINAL STATS")
        logger.info("=" * 60)
        logger.info("Total Bets:   %d", self.positions.total_bets)
        logger.info("Win Rate:     %.1f%%", self.positions.win_rate)
        logger.info("Paper P&L:    $%.2f", self.positions.total_pnl)
        logger.info("=" * 60)


# ═══════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    bot = Bot()
    try:
        bot.start()
    except KeyboardInterrupt:
        logger.info("Stopped.")