"""
signals/engine.py — The prediction logic.

HOW IT WORKS (plain English):

1. Each "signal" looks at one aspect of market data and outputs
   a number between -1 (strongly Down) and +1 (strongly Up).

2. Each signal has a "weight" that starts at 1.0 and adjusts
   based on its real-world win rate. Signals that are right more
   often get more say. Signals that are wrong get ignored.

3. All signals are combined into a single probability:
   prob_up = sigmoid(weighted_sum_of_signals)

4. We compare that probability to what Polymarket is pricing:
   edge = prob_up - market_implied_prob

5. If edge > MIN_EDGE → fire signal.

WHY THESE SPECIFIC SIGNALS:

- CVD (Cumulative Volume Delta): tracks whether real money is
  buying or selling. This is the #1 predictor for short-term direction.
  If more contracts are being bought (market orders hitting asks) than
  sold → price tends to continue up over the next 5 minutes.

- OB Imbalance: if the order book has 3x more buy orders than sell
  orders sitting there, market makers are defending a price level.
  Price tends to move toward the thin side.

- Funding Rate: when funding is very positive, longs are crowded.
  Crowded longs = fragile. Any move down triggers stop-losses.
  When funding flips quickly, that's a warning sign.

- Perp Basis: if perpetual futures trade at a premium to spot,
  futures buyers are paying up = bullish near term. Discount = bearish.

- Micro momentum: simple 60-second price change. Momentum persists
  over 5-minute windows more than people expect.
"""

import math
import time
import logging
import collections
from dataclasses import dataclass, field

import config
from core.feed import State

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
#  SIGNAL RESULT
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Signal:
    name:      str
    score:     float   # -1.0 (strong Down) to +1.0 (strong Up)
    weight:    float   # 0.6 to 1.5, based on historical accuracy
    raw:       dict = field(default_factory=dict)

    @property
    def weighted_score(self) -> float:
        return self.score * self.weight


@dataclass
class Prediction:
    prob_up:         float   # 0..1 — model's probability BTC goes up
    market_implied:  float   # 0..1 — what Polymarket is pricing
    edge:            float   # prob_up - market_implied
    direction:       str     # "Up" | "Down" | "NoEdge"
    confidence:      float   # how strong the prediction is
    signals:         list    # list of Signal objects
    market_id:       str = ""
    market_question: str = ""
    token_yes:       str = ""
    token_no:        str  = ""


# ═══════════════════════════════════════════════════════════════════════════
#  SIGNAL TRACKER  (W/L + adaptive weight)
# ═══════════════════════════════════════════════════════════════════════════

class SignalTracker:
    """
    Tracks win/loss for each signal and adjusts its weight.

    Weight formula:
      - 50% accuracy → weight = 1.0 (neutral, no adjustment)
      - 70% accuracy → weight = 1.4 (trust it more)
      - 30% accuracy → weight = 0.6 (near-ignore it)
    """

    def __init__(self, name: str):
        self.name    = name
        self.history = collections.deque(maxlen=config.WL_WINDOW)
        self.weight  = 1.0   # starts neutral

    def record(self, won: bool):
        self.history.append(1 if won else 0)
        self._update_weight()

    def _update_weight(self):
        if len(self.history) < 5:
            return   # not enough data yet
        rate = sum(self.history) / len(self.history)
        # Linear scale: 0% → 0.6, 50% → 1.0, 100% → 1.5
        self.weight = 0.6 + (rate * 0.9)

    @property
    def wins(self):  return sum(self.history)
    @property
    def total(self): return len(self.history)
    @property
    def rate(self):  return self.wins / self.total if self.total else 0.5
    @property
    def rate_pct(self): return self.rate * 100


# ═══════════════════════════════════════════════════════════════════════════
#  INDIVIDUAL SIGNAL FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════

def _sigmoid(x: float) -> float:
    """Maps any number to 0..1. Used to convert raw scores to probabilities."""
    return 1 / (1 + math.exp(-x))

def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def signal_cvd_1m(state: State) -> Signal:
    """
    1-minute CVD. Most responsive, but also most noisy.
    Strong positive = aggressive buying in last 60 seconds.
    """
    with state.lock:
        score = state.cvd.get(1, 0.0)
    return Signal("cvd_1m", _clamp(score * 2.0), 1.0, {"cvd_1m": score})


def signal_cvd_3m(state: State) -> Signal:
    """
    3-minute CVD. Better signal-to-noise than 1m.
    This is the main CVD signal we weight most.
    """
    with state.lock:
        score = state.cvd.get(3, 0.0)
    return Signal("cvd_3m", _clamp(score * 1.8), 1.0, {"cvd_3m": score})


def signal_cvd_5m(state: State) -> Signal:
    """
    5-minute CVD. Slowest but confirms trend.
    Agreement between 1m, 3m, 5m CVD = high conviction.
    """
    with state.lock:
        score = state.cvd.get(5, 0.0)
    return Signal("cvd_5m", _clamp(score * 1.5), 1.0, {"cvd_5m": score})


def signal_cvd_agreement(state: State) -> Signal:
    """
    Are all CVD windows pointing the same direction?
    If 1m, 3m, and 5m all positive → strong Up signal.
    If they disagree → weak/neutral.
    This is a "meta-signal" on top of CVD.
    """
    with state.lock:
        cvd_1 = state.cvd.get(1, 0.0)
        cvd_3 = state.cvd.get(3, 0.0)
        cvd_5 = state.cvd.get(5, 0.0)

    signs  = [1 if c > 0.02 else (-1 if c < -0.02 else 0) for c in [cvd_1, cvd_3, cvd_5]]
    all_up   = all(s > 0 for s in signs)
    all_down = all(s < 0 for s in signs)

    if all_up:
        magnitude = (cvd_1 + cvd_3 + cvd_5) / 3
        return Signal("cvd_agree", _clamp(magnitude * 3.0), 1.2, {"agree": "up"})
    if all_down:
        magnitude = (cvd_1 + cvd_3 + cvd_5) / 3
        return Signal("cvd_agree", _clamp(magnitude * 3.0), 1.2, {"agree": "down"})

    return Signal("cvd_agree", 0.0, 1.0, {"agree": "mixed"})


def signal_ob_imbalance_shallow(state: State) -> Signal:
    """
    Top-5 OB imbalance. Reflects immediate short-term pressure.
    Very positive = a lot of buy orders sitting just below price.
    """
    with state.lock:
        imb = state.ob_imbalance.get(5, 0.0)
    return Signal("ob_shallow", _clamp(imb * 1.5), 1.0, {"imb_5": imb})


def signal_ob_imbalance_deep(state: State) -> Signal:
    """
    Top-20 OB imbalance. Reflects structural positioning.
    A deep imbalance that isn't reflected in shallow = institutional wall.
    """
    with state.lock:
        imb_5  = state.ob_imbalance.get(5,  0.0)
        imb_20 = state.ob_imbalance.get(20, 0.0)

    # Deep imbalance with shallow agreement = stronger signal
    score = (imb_20 * 0.6) + (imb_5 * 0.4)
    return Signal("ob_deep", _clamp(score * 1.3), 1.0, {"imb_20": imb_20})


def signal_ob_divergence(state: State) -> Signal:
    """
    Shallow vs deep OB diverge → someone is hiding a big order.
    E.g. deep bids but shallow asks = hidden seller about to dump.
    This is a contrarian signal when detected.
    """
    with state.lock:
        imb_5  = state.ob_imbalance.get(5,  0.0)
        imb_20 = state.ob_imbalance.get(20, 0.0)

    divergence = imb_5 - imb_20   # positive = shallow bullish but deep bearish
    if abs(divergence) < 0.15:
        return Signal("ob_diverge", 0.0, 0.8, {"div": divergence})

    # Contrarian: if shallow very bullish but deep very bearish → Down signal
    score = -_clamp(divergence * 2.0)
    return Signal("ob_diverge", score, 0.8, {"div": divergence})


def signal_funding_rate(state: State) -> Signal:
    """
    Funding rate indicates crowding.
    Very positive funding → longs overcrowded → mean-reversion DOWN risk.
    Very negative funding → shorts overcrowded → squeeze UP risk.

    Note: this is a medium-term signal (hours, not minutes), so
    we give it lower weight for 5-min prediction.
    """
    with state.lock:
        rate  = state.funding_rate
        delta = state.funding_delta

    # Extreme funding → contrarian signal
    if rate > 0.0005:    # very positive (longs paying a lot)
        score = -min(rate / 0.001, 1.0)
    elif rate < -0.0005: # very negative (shorts paying)
        score = min(abs(rate) / 0.001, 1.0)
    else:
        score = 0.0

    # Rapid funding change is more actionable than absolute level
    if abs(delta) > 0.0001:
        score += _clamp(-delta * 5000)   # rapid increase → extra bearish

    return Signal("funding", _clamp(score), 0.7, {"rate": rate, "delta": delta})


def signal_perp_basis(state: State) -> Signal:
    """
    Perp basis = (perp_price - spot_price) / spot_price.
    Positive basis → futures buyers paying premium → bullish near-term.
    Negative basis → futures sellers driving price down → bearish.

    For 5-min windows, this captures whether leveraged traders are
    positioning long or short right now.
    """
    with state.lock:
        basis = state.perp_basis

    # Basis of 0.01% = neutral. Beyond that → signal.
    if abs(basis) < 0.0001:
        return Signal("perp_basis", 0.0, 0.8, {"basis": basis})

    score = _clamp(basis * 200)   # scale: 0.005 basis → score of 1.0
    return Signal("perp_basis", score, 0.8, {"basis": basis})


def signal_micro_momentum(state: State) -> Signal:
    """
    60-second price momentum.
    Simple but effective: if price moved up 0.1% in 60s, it tends
    to continue for another 1-2 minutes before mean-reverting.
    We capture the continuation, not the reversal.
    """
    now = time.time()
    with state.lock:
        trades  = list(state.trades)
        current = state.spot_price

    if not trades or current == 0:
        return Signal("momentum_60s", 0.0, 1.0, {})

    # Find price 60 seconds ago
    cutoff = now - 60
    old_trades = [t for t in trades if t.timestamp <= cutoff]
    if not old_trades:
        return Signal("momentum_60s", 0.0, 1.0, {})

    old_price = old_trades[-1].price
    if old_price == 0:
        return Signal("momentum_60s", 0.0, 1.0, {})

    pct_change = (current - old_price) / old_price
    # Scale: 0.1% move → score of 1.0
    score = _clamp(pct_change * 1000)
    return Signal("momentum_60s", score, 1.0, {"pct_change": pct_change})


def signal_trade_size_skew(state: State) -> Signal:
    """
    Are the big trades going up or down?
    Large trades (above median size) are more likely to be informed.
    If big trades are mostly buys → smart money is buying → Up signal.
    """
    now = time.time()
    with state.lock:
        recent = [t for t in state.trades if t.timestamp >= now - 120]

    if len(recent) < 10:
        return Signal("size_skew", 0.0, 0.9, {})

    # Find median trade size
    sizes  = sorted(t.qty for t in recent)
    median = sizes[len(sizes) // 2]

    # Large trades only
    large = [t for t in recent if t.qty >= median * 1.5]
    if not large:
        return Signal("size_skew", 0.0, 0.9, {})

    buy_vol  = sum(t.qty for t in large if t.is_buyer)
    sell_vol = sum(t.qty for t in large if not t.is_buyer)
    total    = buy_vol + sell_vol

    score = _clamp((buy_vol - sell_vol) / (total + 1e-9) * 1.5)
    return Signal("size_skew", score, 0.9, {"large_buy_pct": buy_vol / (total + 1e-9)})


# ═══════════════════════════════════════════════════════════════════════════
#  ENSEMBLE ENGINE
# ═══════════════════════════════════════════════════════════════════════════

ALL_SIGNAL_FUNCTIONS = [
    signal_cvd_1m,
    signal_cvd_3m,
    signal_cvd_5m,
    signal_cvd_agreement,
    signal_ob_imbalance_shallow,
    signal_ob_imbalance_deep,
    signal_ob_divergence,
    signal_funding_rate,
    signal_perp_basis,
    signal_micro_momentum,
    signal_trade_size_skew,
]


class SignalEngine:
    """
    Runs all signals, combines them, compares to Polymarket odds,
    and returns a Prediction.
    """

    def __init__(self):
        self.trackers = {
            fn.__name__.replace("signal_", ""): SignalTracker(fn.__name__.replace("signal_", ""))
            for fn in ALL_SIGNAL_FUNCTIONS
        }
        # Map tracker names back to signal names as they appear in Signal.name
        self._name_map = {
            fn.__name__.replace("signal_", ""): None
            for fn in ALL_SIGNAL_FUNCTIONS
        }

    def run(self, state: State, market_implied: float,
            market_id: str = "", market_question: str = "",
            token_yes: str = "", token_no: str = "") -> Prediction:

        # 1. Compute all signals
        raw_signals = []
        for fn in ALL_SIGNAL_FUNCTIONS:
            try:
                sig = fn(state)
                # Apply learned weight from tracker
                tracker_key = sig.name
                tracker = self.trackers.get(tracker_key)
                if tracker:
                    sig.weight = tracker.weight
                raw_signals.append(sig)
            except Exception as e:
                logger.warning("Signal %s failed: %s", fn.__name__, e)

        # 2. Compute weighted sum of scores
        total_weight = sum(s.weight for s in raw_signals)
        if total_weight < 0.01:
            return Prediction(0.5, market_implied, 0.0, "NoEdge", 0.5, raw_signals)

        weighted_sum = sum(s.weighted_score for s in raw_signals)
        normalised   = weighted_sum / total_weight  # -1 to +1

        # 3. Convert to probability using sigmoid
        # Scale factor of 3: a normalised score of 0.5 → prob of ~0.82
        # This feels aggressive but 5-min markets are high variance
        prob_up = _sigmoid(normalised * 3.0)

        # 4. Calculate edge vs Polymarket
        edge_up   = prob_up - market_implied
        edge_down = (1 - prob_up) - (1 - market_implied)

        # 5. Determine direction and confidence
        if edge_up > config.MIN_EDGE and prob_up >= config.MIN_CONFIDENCE:
            direction   = "Up"
            edge        = edge_up
            confidence  = prob_up
        elif edge_down > config.MIN_EDGE and (1 - prob_up) >= config.MIN_CONFIDENCE:
            direction   = "Down"
            edge        = edge_down
            confidence  = 1 - prob_up
        else:
            direction   = "NoEdge"
            edge        = max(edge_up, edge_down)
            confidence  = max(prob_up, 1 - prob_up)

        return Prediction(
            prob_up        = prob_up,
            market_implied = market_implied,
            edge           = edge,
            direction      = direction,
            confidence     = confidence,
            signals        = raw_signals,
            market_id      = market_id,
            market_question= market_question,
            token_yes      = token_yes,
            token_no       = token_no,
        )

    def record_outcome(self, prediction: Prediction, won: bool):
        """
        Called when a market resolves.
        Updates W/L for every signal that contributed to the prediction.
        Only records signals that had a non-zero score (they actually contributed).
        """
        for sig in prediction.signals:
            tracker = self.trackers.get(sig.name)
            if tracker and abs(sig.score) > 0.05:
                tracker.record(won)
        logger.info("Outcome recorded: %s — signals updated", "WIN" if won else "LOSS")

    def get_tracker_summary(self) -> list[dict]:
        """Returns list of dicts for dashboard display."""
        result = []
        for name, trk in self.trackers.items():
            result.append({
                "name":   name,
                "wins":   trk.wins,
                "total":  trk.total,
                "rate":   trk.rate_pct,
                "weight": round(trk.weight, 2),
            })
        return sorted(result, key=lambda x: -x["rate"])