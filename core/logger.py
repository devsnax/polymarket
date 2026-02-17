"""
core/logger.py — Saves everything to CSV.

Two files:
  signals.csv  → every prediction the bot makes (even NoEdge ones)
  outcomes.csv → resolved outcomes matched to predictions

Why this matters:
  After a week of running, you can open signals.csv in Excel and see
  whether your model is actually right when it says 60% confidence.
  If it says 60% but you're only right 52% of the time, the model
  is overconfident and MIN_CONFIDENCE should be raised.
  This is called "calibration" and it's how you know if the bot
  has real edge or is just fooling itself.
"""

import csv
import os
import logging
import threading
from datetime import datetime, timezone

import config
from signals.engine import Prediction

logger = logging.getLogger(__name__)


class BotLogger:

    def __init__(self):
        os.makedirs(config.LOG_DIR, exist_ok=True)
        os.makedirs(config.DATA_DIR, exist_ok=True)
        self._lock = threading.Lock()
        self._init_files()

    def _init_files(self):
        """Create CSV files with headers if they don't exist."""
        sig_path = config.SIGNALS_LOG
        out_path = config.OUTCOMES_LOG

        if not os.path.exists(sig_path):
            with open(sig_path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow([
                    "timestamp", "market_id", "market_question",
                    "direction", "prob_up", "market_implied", "edge",
                    "confidence",
                    # individual signal scores
                    "cvd_1m", "cvd_3m", "cvd_5m", "cvd_agree",
                    "ob_shallow", "ob_deep", "ob_diverge",
                    "funding", "perp_basis", "momentum_60s", "size_skew",
                ])
            logger.info("Created %s", sig_path)

        if not os.path.exists(out_path):
            with open(out_path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow([
                    "timestamp", "market_id", "direction",
                    "confidence", "edge", "outcome", "won",
                    "pnl_paper"  # paper P&L in USD
                ])
            logger.info("Created %s", out_path)

    def log_signal(self, pred: Prediction):
        """Log every prediction (call this every tick, even NoEdge)."""
        ts = datetime.now(timezone.utc).isoformat()

        # Build a dict of signal scores for easy lookup
        scores = {s.name: round(s.score, 4) for s in pred.signals}

        row = [
            ts,
            pred.market_id,
            pred.market_question[:60] if pred.market_question else "",
            pred.direction,
            round(pred.prob_up, 4),
            round(pred.market_implied, 4),
            round(pred.edge, 4),
            round(pred.confidence, 4),
            # signal scores in fixed order
            scores.get("cvd_1m",      0),
            scores.get("cvd_3m",      0),
            scores.get("cvd_5m",      0),
            scores.get("cvd_agree",   0),
            scores.get("ob_shallow",  0),
            scores.get("ob_deep",     0),
            scores.get("ob_diverge",  0),
            scores.get("funding",     0),
            scores.get("perp_basis",  0),
            scores.get("momentum_60s",0),
            scores.get("size_skew",   0),
        ]

        with self._lock:
            with open(config.SIGNALS_LOG, "a", newline="") as f:
                csv.writer(f).writerow(row)

    def log_outcome(self, market_id: str, direction: str,
                    confidence: float, edge: float,
                    won: bool, bet_usd: float):
        """Log the result of a resolved position."""
        ts  = datetime.now(timezone.utc).isoformat()
        pnl = bet_usd * (1 / confidence - 1) if won else -bet_usd

        row = [
            ts, market_id, direction,
            round(confidence, 4), round(edge, 4),
            "win" if won else "loss", int(won),
            round(pnl, 4),
        ]

        with self._lock:
            with open(config.OUTCOMES_LOG, "a", newline="") as f:
                csv.writer(f).writerow(row)

        logger.info("Outcome logged: %s %s | won=%s | pnl=$%.2f",
                    direction, market_id[:12], won, pnl)