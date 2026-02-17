"""
main.py — Start the bot.

Run with:
  python main.py

Then open http://localhost:5000 in your browser.

What happens:
  1. BinanceFeed starts — connects WebSocket, receives live trades + OB
  2. RestPoller starts — fetches funding rate every 30s
  3. Main loop ticks every 10s:
     a. Compute CVD + OB imbalance from raw data
     b. Fetch active BTC markets from Polymarket
     c. Run signal engine → get prediction
     d. If edge found → open paper position
     e. Check expired positions → resolve + update W/L
     f. Log everything to CSV
  4. Dashboard serves http://localhost:5000
"""

import time
import logging
import sys
import os

# Make sure all submodules are importable
sys.path.insert(0, os.path.dirname(__file__))

import config
from core.feed      import State, BinanceFeed, RestPoller, MetricComputer, PolymarketFeed
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
#  BOT
# ═══════════════════════════════════════════════════════════════════════════

class Bot:

    def __init__(self):
        logger.info("=" * 60)
        logger.info("  POLYMARKET BTC SIGNAL BOT")
        logger.info("  Paper trading mode — signals only")
        logger.info("=" * 60)

        # Shared state (lives in RAM, updated by WebSocket)
        self.state       = State()

        # Data feeds
        self.feed        = BinanceFeed(self.state)
        self.poller      = RestPoller(self.state)
        self.metrics     = MetricComputer(self.state)
        self.poly        = PolymarketFeed()

        # Signal + position engine
        self.engine      = SignalEngine()
        self.bot_logger  = BotLogger()
        self.positions   = PositionTracker(self.bot_logger, self.engine)

        # Last prediction (exposed to dashboard)
        self.last_prediction = None

    def start(self):
        # Start data feeds
        self.feed.start()
        self.poller.start()

        # Start dashboard
        set_bot(self)
        run_dashboard(config.DASHBOARD_HOST, config.DASHBOARD_PORT)

        # Wait for WebSocket to connect and fill a little data
        logger.info("Waiting 8s for WebSocket to fill with data...")
        time.sleep(8)

        # Main loop
        logger.info("Starting main loop (ticking every %ds)", config.TICK_INTERVAL)
        while True:
            try:
                self._tick()
            except KeyboardInterrupt:
                logger.info("Shutdown requested.")
                self._shutdown()
                break
            except Exception as e:
                logger.error("Tick error: %s", e, exc_info=True)
            time.sleep(config.TICK_INTERVAL)

    def _tick(self):
        # 1. Compute derived metrics (CVD, OB imbalance) from raw WebSocket data
        self.metrics.compute()

        # 2. Get current state snapshot
        with self.state.lock:
            spot_price   = self.state.spot_price
            ws_connected = self.state.ws_connected

        if not ws_connected:
            logger.warning("WebSocket not connected — skipping tick")
            return

        if spot_price == 0:
            logger.warning("No price data yet — skipping tick")
            return

        # 3. Fetch Polymarket markets
        markets = self.poly.get_btc_markets()
        if not markets:
            logger.debug("No BTC markets found on Polymarket")
            # Still run signals, just no edge comparison
            self._run_signals_no_market(spot_price)
            return

        # 4. Use the first (most imminent) market
        market           = markets[0]
        market_id        = market.get("id", "")
        market_question  = market.get("question", "BTC Up or Down")
        market_implied   = self.poly.get_implied_prob(market)
        token_yes, token_no = self.poly.get_token_ids(market)

        logger.info("Market: %s | Implied: %.3f", market_question[:50], market_implied)

        # 5. Run signal engine
        prediction = self.engine.run(
            state            = self.state,
            market_implied   = market_implied,
            market_id        = market_id,
            market_question  = market_question,
            token_yes        = token_yes,
            token_no         = token_no,
        )
        self.last_prediction = prediction

        # 6. Log signal (always, even NoEdge)
        self.bot_logger.log_signal(prediction)

        # 7. Log prediction to console
        direction_str = prediction.direction
        logger.info(
            "PREDICTION: %-8s | prob=%.3f | implied=%.3f | edge=+%.3f | conf=%.3f",
            direction_str, prediction.prob_up, market_implied,
            prediction.edge, prediction.confidence
        )

        # 8. Open position if there's edge
        if prediction.direction in ("Up", "Down"):
            self.positions.open_position(
                market_id       = market_id,
                question        = market_question,
                direction       = prediction.direction,
                confidence      = prediction.confidence,
                edge            = prediction.edge,
                entry_price     = spot_price,
                token_yes       = token_yes,
                token_no        = token_no,
                prediction      = prediction,
            )

        # 9. Resolve expired positions (check outcome by price movement)
        self.positions.check_resolutions(spot_price)

        # 10. Log summary
        open_count = len(self.positions.get_open_positions())
        logger.info(
            "Open: %d | Bets: %d | Win%%: %.1f | PnL: $%.2f",
            open_count, self.positions.total_bets,
            self.positions.win_rate, self.positions.total_pnl
        )

    def _run_signals_no_market(self, spot_price: float):
        """Run signals even when no Polymarket market is found."""
        from signals.engine import Prediction
        prediction = self.engine.run(
            state          = self.state,
            market_implied = 0.5,
        )
        self.last_prediction = prediction

    def _shutdown(self):
        logger.info("Shutting down...")
        self.feed.stop()
        self.poller.stop()
        logger.info("Final P&L: $%.2f | Win rate: %.1f%% | Total bets: %d",
                    self.positions.total_pnl,
                    self.positions.win_rate,
                    self.positions.total_bets)


# ═══════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    bot = Bot()
    try:
        bot.start()
    except KeyboardInterrupt:
        logger.info("Stopped.")