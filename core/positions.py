"""
core/positions.py — Tracks paper positions and resolves them.

A "position" is opened when the signal engine fires a prediction
with enough edge. It's resolved 5 minutes later by checking
whether BTC actually went up or down.

This is purely paper trading — no real money moves.
When you're ready to go live, wallet.py handles the real orders.
"""

import time
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import config

logger = logging.getLogger(__name__)


@dataclass
class Position:
    market_id:    str
    question:     str
    direction:    str    # "Up" | "Down"
    confidence:   float
    edge:         float
    entry_price:  float  # BTC spot price when opened
    bet_usd:      float
    token_yes:    str
    token_no:     str
    opened_at:    float  = field(default_factory=time.time)
    eta_seconds:  int    = 300   # 5-minute window
    resolved:     bool   = False
    won:          Optional[bool] = None

    @property
    def age_seconds(self) -> float:
        return time.time() - self.opened_at

    @property
    def seconds_remaining(self) -> float:
        return max(0, self.eta_seconds - self.age_seconds)

    @property
    def is_expired(self) -> bool:
        return self.age_seconds >= self.eta_seconds + 30  # 30s grace period

    def eta_str(self) -> str:
        s = int(self.seconds_remaining)
        m, sec = divmod(s, 60)
        return f"{m}m{sec:02d}s"


class PositionTracker:
    """
    Manages all open paper positions.
    Checks for resolution every tick.
    """

    def __init__(self, bot_logger, signal_engine):
        self.positions:     list[Position] = []
        self._lock         = threading.Lock()
        self._logger       = bot_logger
        self._engine       = signal_engine
        self.total_pnl     = 0.0
        self.total_wins    = 0
        self.total_losses  = 0

    def open_position(self, market_id: str, question: str,
                      direction: str, confidence: float, edge: float,
                      entry_price: float, token_yes: str, token_no: str,
                      prediction=None) -> Optional[Position]:
        """Open a new paper position if we don't already have one for this market."""
        with self._lock:
            # Don't open duplicate positions on same market
            existing = [p for p in self.positions
                        if p.market_id == market_id and not p.resolved]
            if existing:
                return None

            # Don't exceed max open positions
            open_count = sum(1 for p in self.positions if not p.resolved)
            if open_count >= config.MAX_OPEN_POSITIONS:
                logger.debug("Max positions reached (%d)", config.MAX_OPEN_POSITIONS)
                return None

            pos = Position(
                market_id   = market_id,
                question    = question,
                direction   = direction,
                confidence  = confidence,
                edge        = edge,
                entry_price = entry_price,
                bet_usd     = config.PAPER_BET_USD,
                token_yes   = token_yes,
                token_no    = token_no,
            )
            self.positions.append(pos)
            logger.info("📌 OPENED: %s %s | conf=%.3f edge=%.3f | entry=$%.2f",
                        direction, question[:40], confidence, edge, entry_price)
            return pos

    def check_resolutions(self, current_price: float):
        """
        For each expired position, check if it won by comparing
        current price to entry price.
        """
        with self._lock:
            for pos in self.positions:
                if pos.resolved or not pos.is_expired:
                    continue

                # Simple resolution: did price go the direction we predicted?
                price_went_up = current_price > pos.entry_price

                if pos.direction == "Up":
                    won = price_went_up
                else:
                    won = not price_went_up

                pos.resolved = True
                pos.won      = won

                # Calculate paper P&L
                # Simplified: bet $5, if win get back based on implied odds
                if won:
                    pnl = pos.bet_usd * (1 / pos.confidence - 1)
                    self.total_wins += 1
                else:
                    pnl = -pos.bet_usd
                    self.total_losses += 1

                self.total_pnl += pnl

                result_emoji = "✅" if won else "❌"
                logger.info(
                    "%s RESOLVED: %s %s | entry=$%.2f exit=$%.2f | pnl=$%.2f | total=$%.2f",
                    result_emoji, pos.direction, pos.question[:35],
                    pos.entry_price, current_price, pnl, self.total_pnl
                )

                # Log outcome to CSV
                self._logger.log_outcome(
                    market_id  = pos.market_id,
                    direction  = pos.direction,
                    confidence = pos.confidence,
                    edge       = pos.edge,
                    won        = won,
                    bet_usd    = pos.bet_usd,
                )

    def get_open_positions(self) -> list[Position]:
        with self._lock:
            return [p for p in self.positions if not p.resolved]

    def get_recent_resolved(self, n: int = 10) -> list[Position]:
        with self._lock:
            resolved = [p for p in self.positions if p.resolved]
            return resolved[-n:]

    @property
    def win_rate(self) -> float:
        total = self.total_wins + self.total_losses
        return (self.total_wins / total * 100) if total else 0.0

    @property
    def total_bets(self) -> int:
        return self.total_wins + self.total_losses