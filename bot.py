import os
import time
import json
import requests
import gspread
import numpy as np
import pandas as pd
from collections import defaultdict, deque
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from google.oauth2.service_account import Credentials
# =========================================================
# CONFIG (UPGRADED CORE CONFIGURATION)
# =========================================================

COINS = [
    "ZEC-USDT", "BNB-USDT", "AVAX-USDT", "LINK-USDT",
    "SUI-USDT", "HYPE-USDT", "TAO-USDT", "ONDO-USDT",
    "VVV-USDT", "AR-USDT", "RENDER-USDT", "ICP-USDT",
    "NEAR-USDT", "BTC-USDT", "ETH-USDT", "SOL-USDT"
]

# =========================================================
# TIME (NEW YORK)
# =========================================================

NY = ZoneInfo("America/New_York")

def now():
    return datetime.now(NY)

def timestamp():
    return now().strftime("%Y-%m-%d %H:%M:%S")


CYCLE_SLEEP = 300  # 5 minutes
MAX_OPEN_TRADES = 3
ENTRY_COOLDOWN = 1800  # 30 min

# Dynamic exit baselines
TP_BASE = 3.0
SL_BASE = -1.5
MAX_HOLD_TIME = 7200

# =========================================================
# STATE MANAGER (FROM WORKING V14)
# =========================================================

trades = {}
last_entry = defaultdict(float)
sheet_queue = deque(maxlen=2000)
price_cache = {}

# =========================================================
# TELEGRAM (FROM WORKING V14)
# =========================================================
TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

def send_telegram(msg):
    try:
        if TOKEN and CHAT_ID:
            # We use json= instead of data= for better handling of variable types
            response = requests.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": msg},
                timeout=10
            )
            # This line will show us if Telegram rejected your token or chat ID
            if not response.ok:
                print(f"Telegram API Error: {response.text}")
    except Exception as e:
        print(f"Telegram network error: {e}")

send_telegram("🤖 Bot 2.0 has successfully booted up on Railway!")

# =========================================================
# GOOGLE SHEETS 
# =========================================================

sheet = None

def flush_sheet():
    if not sheet or not sheet_queue:
        return
    try:
        sheet.append_row(sheet_queue.popleft())
    except Exception as e:
        print("SHEET ERROR:", e)

def log_sheet(row):
    sheet_queue.append(row)

try:
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    if creds_json:
        creds = Credentials.from_service_account_info(
            json.loads(creds_json),
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        sheet = gspread.authorize(creds)\
            .open_by_key("1lDJsm0sZCN1kK_a3rvV4-QVF2IJ7BntP_Ovf6NgQums")\
            .sheet1
        print("Sheets connected OK")
    else:
        print("Sheets disabled")
except Exception as e:
    print("Sheets init failed:", e)
    sheet = None

# =========================================================
# BULLETPROOF DATA FEED MATRIX (FROM WORKING V14)
# =========================================================

def safe_get(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json"
    }
    for _ in range(2):
        try:
            r = requests.get(url, headers=headers, timeout=8)
            if r.status_code == 200:
                return r.json()
        except:
            time.sleep(0.8)
    return None

def fetch(symbol):
    try:
        ticker = str(symbol).strip().upper()

        if "-" not in ticker and "USDT" in ticker:
            ticker = ticker.replace("USDT", "-USDT")

        if ticker == "TON-USDT":
            ticker = "TONCOIN-USDT"

        url = f"https://api.kucoin.com/api/v1/market/candles?type=5min&symbol={ticker}"

        data = safe_get(url)

        if not data or "data" not in data or data["data"] is None:
            return price_cache.get(symbol)

        closes = []

        for i in data["data"][:120]:
            try:
                closes.append(float(i[2]))
            except:
                continue

        print(f"{symbol}: Parsed {len(closes)} candles")

        if len(closes) < 50:
            return price_cache.get(symbol)

        closes.reverse()
        price_cache[symbol] = closes
        return closes

    except Exception as e:
        print(f"{symbol} fetch error:", e)
        return price_cache.get(symbol)

# =========================================================
# INDICATORS (FROM WORKING V14 METRIC WEIGHTS)
# =========================================================

def ema(data, period):
    series = pd.Series(data).dropna()
    if len(series) < period:
        return None
    return series.ewm(span=period, adjust=False).mean().iloc[-1]

def rsi(data):
    diff = np.diff(data)
    gain = np.mean(np.clip(diff, 0, None))
    loss = np.mean(np.clip(-diff, 0, None))
    if loss == 0:
        return 100
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def volatility(data):
    return float(np.std(data[-30:]) / np.mean(data[-30:]))

# =========================================================
# UPGRADED SCORE ENGINE (NEW BOT CORE BIAS)
# =========================================================

def calculate_score(e21, e50, rsi_val, vol, price, prev_price):
    if e21 > e50:
        score = 3
        if rsi_val > 55: score += 2
        elif rsi_val < 45: score += 1
        if price > prev_price: score += 1
    else:
        score = -3
        if rsi_val < 45: score -= 2
        elif rsi_val > 55: score -= 1
        if price < prev_price: score -= 1

    trend_strength = abs(e21 - e50) / price
    if trend_strength > 0.01:
        score += 2 if score > 0 else -2
    elif trend_strength > 0.005:
        score += 1 if score > 0 else -1

    if vol > 0.002:
        score += 1 if score > 0 else -1

    if abs(price - e21) / price < 0.01:
        score += 1 if score > 0 else -1

    return round(score, 2)

# =========================================================
# POSITION OPERATIONS (NEW UPGRADED BOT EXECUTION)
# =========================================================

def open_trade(symbol, direction, entry, score, vol, rsi_val):
    if len(trades) >= MAX_OPEN_TRADES:
        return
    if time.time() - last_entry[symbol] < ENTRY_COOLDOWN:
        return

    multiplier = max(1.0, vol / 0.0025)
    sl_pct = abs(SL_BASE) * multiplier
    tp_pct = TP_BASE * multiplier

    sl = entry * (1 - (sl_pct / 100)) if direction == "LONG" else entry * (1 + (sl_pct / 100))
    tp = entry * (1 + (tp_pct / 100)) if direction == "LONG" else entry * (1 - (tp_pct / 100))

    trades[symbol] = {
        "symbol": symbol,
        "direction": direction,
        "entry": entry,
        "sl_pct": sl_pct,
        "tp_pct": tp_pct,
        "score": score,
        "time": time.time(),
        "highest_price": entry,
        "lowest_price": entry,
        "rsi": rsi_val  # Store for closing row
    }

    last_entry[symbol] = time.time()
    msg = f"📌 BOT 2 POSITION MIGRATED\n{symbol} {direction} Opened\nScore: {score}\nVol: {vol:.5f}\nEntry: {entry:.4f}\nSL: {sl:.4f}\nTP: {tp:.4f}"
    send_telegram(msg)

    # EXACT BOT 4 SCHEMA SYNC (Splits timestamp, places BOT Name in Column A)
    now_ny = datetime.now(NY)
    log_sheet([
        "BOT 2",                                      # A: BOT NAME
        now_ny.strftime("%Y-%m-%d"),                  # B: DATE
        now_ny.strftime("%H:%M:%S"),                  # C: TIME
        symbol,                                       # D: COIN
        "LONG" if direction == "LONG" else "SHORT",   # E: SIDE (LONG/SHORT)
        int(score),                                   # F: SCORE (Raw clean integer)
        round(float(entry), 5),                       # G: ENTRY PRICE
        round(float(tp), 5),                          # H: T/P (Aligned with Bot 4)
        round(float(sl), 5),                          # I: S/L (Aligned with Bot 4)
        round(float(rsi_val), 2),                     # J: RSI
        "N/A",                                        # K: Z-SCORE (Bot 2 bypass placeholder)
        "N/A",                                        # L: ADX (Bot 2 bypass placeholder)
        "OPEN",                                       # M: STATUS
        "N/A"                                         # N: PnL%
    ])

def close_trade(symbol, price, reason):
    if symbol not in trades:
        return
    t = trades[symbol]
    entry = t["entry"]
    direction = t["direction"]
    
    pnl_pct = ((price - entry) / entry) * 100.0 if direction == "LONG" else ((entry - price) / entry) * 100.0
    msg = f"🏁 BOT 2 POSITION CLOSED\n{symbol} Closed ({reason})\nExit Price: {price:.4f}\nPnL: {pnl_pct:.2f}%"
    send_telegram(msg)

    # EXACT BOT 4 SCHEMA SYNC FOR EXITS
    now_ny = datetime.now(NY)
    original_sl = entry * (1 - (t["sl_pct"] / 100)) if direction == "LONG" else entry * (1 + (t["sl_pct"] / 100))
    original_tp = entry * (1 + (t["tp_pct"] / 100)) if direction == "LONG" else entry * (1 - (t["tp_pct"] / 100))

    log_sheet([
        "BOT 2",                                      # A: BOT NAME
        now_ny.strftime("%Y-%m-%d"),                  # B: DATE
        now_ny.strftime("%H:%M:%S"),                  # C: TIME
        symbol,                                       # D: COIN
        "LONG" if direction == "LONG" else "SHORT",   # E: SIDE (LONG/SHORT)
        int(t["score"]),                              # F: SCORE
        round(float(entry), 5),                       # G: ENTRY PRICE
        round(float(original_sl), 5),                 # H: S/L
        round(float(original_tp), 5),                 # I: T/P
        round(float(t["rsi"]), 2),                    # J: RSI
        "N/A",                                        # K: Z-SCORE
        "N/A",                                        # L: ADX
        "CLOSED",                                     # M: STATUS
        f"{pnl_pct:.2f}%"                             # N: PnL%
    ])
    del trades[symbol]

# =========================================================
# UPGRADED TRADE MANAGEMENT (V14 HYBRID TRAILING STOP)
# =========================================================

def manage_trade(symbol, price):
    if symbol not in trades:
        return
    t = trades[symbol]

    pnl = ((price - t["entry"]) / t["entry"]) * 100
    if t["direction"] == "SHORT":
        pnl = -pnl

    if pnl >= t["tp_pct"]:
        close_trade(symbol, price, "TAKE PROFIT")
        return

    if t["direction"] == "LONG":
        if price > t["highest_price"]:
            t["highest_price"] = price
        current_sl_price = t["highest_price"] * (1 - (t["sl_pct"] / 100))
        if price <= current_sl_price:
            close_trade(symbol, price, "TRAILING STOP LOSS")

    elif t["direction"] == "SHORT":
        if price < t["lowest_price"]:
            t["lowest_price"] = price
        current_sl_price = t["lowest_price"] * (1 + (t["sl_pct"] / 100))
        if price >= current_sl_price:
            close_trade(symbol, price, "TRAILING STOP LOSS")

    if time.time() - t["time"] > MAX_HOLD_TIME:
        close_trade(symbol, price, "TIMEOUT")

# =========================================================
# MAIN LOOP UNBROKEN MATRIX (V14 ARCHITECTURE ENGINE)
# =========================================================

if __name__ == "__main__":
    send_telegram("🚀 Upgraded Bot 2 Online (V14 Fixed Core Engine Live)")

    while True:
        try:
            flush_sheet()
            print(f"\n--- Cycle Start: {timestamp()} ---")
            current_prices = {}

            for coin in COINS:
                closes = fetch(coin)
                print(f"{coin}: {'OK' if closes else 'NO DATA'}")
                if not closes:
                    continue

                price = closes[-1]
                prev_price = closes[-2]
                current_prices[coin] = price

                # Check if a position is running and manage it with live candle ticks
                if coin in trades:
                    manage_trade(coin, price)
                    continue

                e21 = ema(closes, 21)
                e50 = ema(closes, 50)
                rsi_val = rsi(closes)
                vol = volatility(closes)

                if None in [e21, e50, rsi_val, vol]:
                    continue

                if score >= 8.5:
                    open_trade(coin, "LONG", price, score, vol, rsi_val)
                elif score <= -8.5:
                    open_trade(coin, "SHORT", price, score, vol, rsi_val)

            print(f"Cycle finished. Active trades: {len(trades)}. Sleeping...")
            time.sleep(CYCLE_SLEEP)

        except Exception as e:
            print("MAIN LOOP FAULT PREVENTED:", e)
            time.sleep(10)
