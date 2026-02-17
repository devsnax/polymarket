# Polymarket BTC Signal Bot — Setup Guide

No jargon. Follow this exactly and it works.

---

## Step 1 — Install Python

If you don't have Python installed:
- Go to https://python.org/downloads
- Download Python 3.11 or newer
- Install it (check "Add Python to PATH" on Windows)

---

## Step 2 — Put the bot files somewhere sensible

Create a folder called `polymarket_bot` on your Desktop (or anywhere).
Put all the bot files in there. The structure should look like:

```
polymarket_bot/
├── main.py
├── config.py
├── requirements.txt
├── core/
│   ├── __init__.py
│   ├── feed.py
│   ├── logger.py
│   └── positions.py
├── signals/
│   ├── __init__.py
│   └── engine.py
└── web/
    ├── __init__.py
    ├── dashboard.py
    └── templates/
        └── dashboard.html
```

---

## Step 3 — Open a terminal in that folder

**Windows:**
  - Open the `polymarket_bot` folder
  - Click the address bar, type `cmd`, press Enter

**Mac:**
  - Right-click the `polymarket_bot` folder
  - "New Terminal at Folder"

---

## Step 4 — Install dependencies (one time only)

Type this and press Enter:
```
pip install -r requirements.txt
```

Wait for it to finish. You'll see a lot of text — that's normal.

---

## Step 5 — Run the bot

```
python main.py
```

You'll see output like:
```
12:34:56 [INFO] main: POLYMARKET BTC SIGNAL BOT
12:34:56 [INFO] BinanceFeed: WebSocket connected ✓
12:34:56 [INFO] Dashboard running at http://localhost:5000
```

---

## Step 6 — Open the dashboard

Open your browser and go to:
```
http://localhost:5000
```

You'll see the dashboard. It updates every 5 seconds automatically.

---

## Step 7 — Leave it running

The bot needs to run continuously to build up W/L history and calibrate
the signals. Let it run for at least a week in signal-only mode.

**To stop it:** press Ctrl+C in the terminal.

---

## What you're looking at in the dashboard

**Top bar:**
- BTC PRICE — current live Bitcoin price
- FUNDING — if very positive (>0.005%), longs are crowded → bearish signal
- BASIS — if positive, futures traders are bullish
- WIN RATE — how often paper predictions were correct
- PAPER P&L — theoretical profit if you'd bet $5 per signal

**Signal accuracy table:**
- Each row is one signal (e.g. CVD, OB imbalance)
- W/L = wins / total resolved
- RATE = win percentage (green = above 50%, red = below)
- MODIFIER = how much this signal's vote is weighted (auto-adjusts)
- ACCURACY BAR = visual indicator

**Current prediction:**
- What the bot thinks will happen in the next 5 minutes
- EDGE = your model's probability minus what Polymarket is pricing
  (Only signals when edge > 4%)
- NO EDGE = the model agrees with the market, no bet

**CVD bars:**
- Cumulative Volume Delta — net buying vs selling pressure
- Green = more buying. Red = more selling.
- Agreement across 1min and 3min = stronger signal

---

## When to consider live trading

Only consider it when ALL of these are true after at least 2 weeks:
1. Win rate is consistently above 55%
2. Paper P&L is positive
3. The bot has made at least 50 paper bets
4. The signals are stable (not flipping every few minutes)

To enable live trading when ready:
1. Go to Polymarket → Settings → API Keys
2. Export your proxy wallet private key
3. Open `config.py` and fill in:
   ```python
   POLY_PRIVATE_KEY = "0x..."
   POLY_FUNDER_ADDR = "0x..."
   LIVE_TRADING     = True
   ```

---

## Common issues

**"ModuleNotFoundError"** — run `pip install -r requirements.txt` again

**"Port already in use"** — another program is using port 5000.
  Change `DASHBOARD_PORT = 5001` in config.py

**Dashboard shows "DISCONNECTED"** — Binance WebSocket dropped.
  The bot reconnects automatically within a few seconds.

**No positions opening** — the model isn't finding enough edge vs
  Polymarket's prices. This is normal — it's being disciplined.
  Lower `MIN_EDGE = 0.03` in config.py to see more signals (more risk).

---

## Moving to Railway later

When you're ready to run 24/7:
1. Push this folder to a GitHub repo
2. Connect Railway to that repo
3. Set start command: `python main.py`
4. Add environment variables for POLY_PRIVATE_KEY etc.
5. Done — Railway handles the rest.