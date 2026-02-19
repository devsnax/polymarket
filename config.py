"""
config.py — All settings in one place.
Change things here, nowhere else.
"""

# ── Price feed ────────────────────────────────────────────────────────────
# Using Coinbase (no geo-restrictions, unlike Binance)
COINBASE_WS_URL   = "wss://ws-feed.exchange.coinbase.com"
COINBASE_REST_URL = "https://api.exchange.coinbase.com"
SYMBOL            = "BTC-USD"          # Coinbase format
SYMBOL_UP         = "BTC-USD"          # same for REST

# ── Polymarket ────────────────────────────────────────────────────────────
GAMMA_URL         = "https://gamma-api.polymarket.com"
CLOB_URL          = "https://clob.polymarket.com"
DATA_URL          = "https://data-api.polymarket.com"

# ── Signal settings ───────────────────────────────────────────────────────
# Minimum edge (your prob - market implied prob) to fire a signal
MIN_EDGE          = 0.04      # 4% edge minimum

# Minimum confidence from the model to fire a signal
MIN_CONFIDENCE    = 0.56      # 56% win probability minimum

# How many recent outcomes to track per signal (rolling window)
WL_WINDOW         = 30

# ── CVD (Cumulative Volume Delta) windows ─────────────────────────────────
CVD_WINDOWS       = [1, 3, 5]   # minutes

# ── Order book depth levels to sample ────────────────────────────────────
OB_DEPTH_LEVELS   = [5, 10, 20]  # top N levels

# ── Funding rate fetch interval ───────────────────────────────────────────
FUNDING_INTERVAL  = 30   # seconds between funding rate fetches

# ── How often the bot "ticks" (evaluates signals) ────────────────────────
TICK_INTERVAL     = 10   # seconds

# ── Web dashboard ─────────────────────────────────────────────────────────
DASHBOARD_HOST    = "0.0.0.0"
DASHBOARD_PORT    = 5000

# ── Data logging ──────────────────────────────────────────────────────────
LOG_DIR           = "logs"
DATA_DIR          = "data"
SIGNALS_LOG       = "logs/signals.csv"    # every signal fired
OUTCOMES_LOG      = "logs/outcomes.csv"   # resolved outcomes

# ── Paper trading ─────────────────────────────────────────────────────────
PAPER_BET_USD     = 5.0    # USD per paper trade
MAX_OPEN_POSITIONS = 3     # don't stack too many at once

# ── Wallet (leave empty until ready to live trade) ───────────────────────
POLY_PRIVATE_KEY  = ""     # export from Polymarket settings when ready
POLY_FUNDER_ADDR  = ""     # your Polymarket proxy wallet address
LIVE_TRADING      = False  # ← flip to True ONLY after profitable backtest