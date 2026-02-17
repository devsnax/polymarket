"""
web/dashboard.py — Real-time web dashboard.

Opens in your browser at http://localhost:5000
Auto-refreshes every 5 seconds.
Shows the same info as the terminal dashboard in the screenshot,
but accessible from any browser (phone, other laptop, etc.)
"""

import json
import logging
from flask import Flask, render_template, jsonify

logger = logging.getLogger(__name__)

app  = Flask(__name__, template_folder="templates")

# Shared reference to bot state — set by main.py before starting Flask
_bot_ref = None

def set_bot(bot):
    global _bot_ref
    _bot_ref = bot


@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/state")
def api_state():
    """JSON endpoint polled by the dashboard every 5 seconds."""
    if not _bot_ref:
        return jsonify({"error": "Bot not running"})

    bot = _bot_ref

    # Signal trackers
    trackers = []
    for name, trk in bot.engine.trackers.items():
        trackers.append({
            "name":   name,
            "wins":   trk.wins,
            "total":  trk.total,
            "rate":   round(trk.rate_pct, 1),
            "weight": round(trk.weight, 2),
        })

    # Active positions
    positions = []
    for pos in bot.positions.get_open_positions():
        positions.append({
            "question":   pos.question,
            "direction":  pos.direction,
            "bet":        pos.bet_usd,
            "confidence": round(pos.confidence, 3),
            "edge":       round(pos.edge, 3),
            "eta":        pos.eta_str(),
            "entry":      round(pos.entry_price, 2),
        })

    # Recent resolved
    recent = []
    for pos in bot.positions.get_recent_resolved(5):
        recent.append({
            "question":  pos.question[:50],
            "direction": pos.direction,
            "won":       pos.won,
            "confidence": round(pos.confidence, 3),
        })

    # Last prediction
    last_pred = bot.last_prediction
    pred_data = {}
    if last_pred:
        pred_data = {
            "direction":      last_pred.direction,
            "prob_up":        round(last_pred.prob_up, 3),
            "market_implied": round(last_pred.market_implied, 3),
            "edge":           round(last_pred.edge, 3),
            "confidence":     round(last_pred.confidence, 3),
        }

    # Feed health
    state = bot.state
    with state.lock:
        feed_ok    = state.ws_connected
        btc_price  = state.spot_price
        funding    = state.funding_rate
        basis      = state.perp_basis
        cvd_1m     = state.cvd.get(1, 0)
        cvd_3m     = state.cvd.get(3, 0)

    return jsonify({
        "trackers":       trackers,
        "positions":      positions,
        "recent":         recent,
        "prediction":     pred_data,
        "feed_ok":        feed_ok,
        "btc_price":      round(btc_price, 2),
        "funding_rate":   round(funding * 100, 5),   # as percentage
        "perp_basis":     round(basis * 100, 5),
        "cvd_1m":         round(cvd_1m, 4),
        "cvd_3m":         round(cvd_3m, 4),
        "win_rate":       round(bot.positions.win_rate, 1),
        "total_pnl":      round(bot.positions.total_pnl, 2),
        "total_bets":     bot.positions.total_bets,
    })


def run_dashboard(host="0.0.0.0", port=5000):
    """Run Flask in a thread (non-blocking)."""
    import threading
    t = threading.Thread(
        target=lambda: app.run(host=host, port=port, debug=False, use_reloader=False),
        daemon=True,
        name="Dashboard",
    )
    t.start()
    logger.info("Dashboard running at http://localhost:%d", port)