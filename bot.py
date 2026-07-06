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



#==========================================================
# GOOGLE SHEETS
# =========================================================
# =========================================================
# GOOGLE SHEETS LOGGER
# =========================================================

def flush_sheet():

    global sheet

    if sheet is None:
        print("SHEET ERROR: No connection")
        return


    if not sheet_queue:
        return


    try:

        while sheet_queue:

            row = sheet_queue.popleft()

            print("Writing row:", row)

            sheet.append_row(row)

            print("Sheet write OK")


    except Exception as e:

        print("SHEET ERROR:", e)



def log_sheet(row):

    try:

        sheet_queue.append(row)

        flush_sheet()

    except Exception as e:

        print("LOG SHEET ERROR:", e)
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

        url = f"https://kucoin.com{ticker}"
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
        "rsi": rsi_val
    }

    last_entry[symbol] = time.time()
    msg = f"📌 BOT 2 MIGRATED SETUP\n{symbol} {direction} Opened\nScore: {score}\nVol: {vol:.5f}\nEntry: {entry:.4f}\nSL: {sl:.4f}\nTP: {tp:.4f}"
    send_telegram(msg)

    # FIXED: Re-mapped array strings to insert perfectly into the unified master 14-column layout
    now_ny = datetime.now(NY)
    log_sheet([
        "BOT 2",                                      # A: BOT NAME
        now_ny.strftime("%Y-%m-%d"),                  # B: DATE
        now_ny.strftime("%H:%M:%S"),                  # C: TIME
        symbol,                                       # D: COIN
        "LONG" if direction == "LONG" else "SHORT",   # E: DIRECTION
        int(score),                                   # F: SCORE
        round(float(entry), 4),                       # G: ENTRY PRICE
        round(float(sl), 4),                          # H: S/L
        round(float(tp), 4),                          # I: T/P
        round(float(rsi_val), 2),                     # J: RSI
        "N/A",                                        # K: Z-SCORE 
        "N/A",                                        # L: ADX 
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

    now_ny = datetime.now(NY)
    log_sheet([
        "BOT 2",
        now_ny.strftime("%Y-%m-%d"),
        now_ny.strftime("%H:%M:%S"),
        symbol,
        "LONG" if direction == "LONG" else "SHORT",
        int(t["score"]),
        round(float(entry), 4),
        round(float(t["entry"] * (1 - (t["sl_pct"] / 100)) if direction == "LONG" else t["entry"] * (1 + (t["sl_pct"] / 100))), 4),  
        round(float(t["entry"] * (1 + (t["tp_pct"] / 100)) if direction == "LONG" else t["entry"] * (1 - (t["tp_pct"] / 100))), 4),
        round(float(t["rsi"]), 2),
        "N/A",
        "N/A",
        "CLOSED",                                     # M: STATUS
        f"{pnl_pct:.2f}%"                             # N: Net PnL% Return
    ])
    del trades[symbol]

# =========================================================
# CORE CORE TICK LOOP LAYER RUNNER
# =========================================================
def run_scanner():
    print(f"[{timestamp()}] Kicking off processing loop for {len(COINS)} tokens...")
    for coin in COINS:
        try:
            data = fetch(coin)
            if not data or len(data) < 60:
                continue
            
            price = data[-1]
            prev_price = data[-2]
            
            e21 = ema(data, 21)
            e50 = ema(data, 50)
            rsi_val = rsi(data)
            vol = volatility(data)
            
            if not e21 or not e50 or not rsi_val:
                continue
                
            score = calculate_score(e21, e50, rsi_val, vol, price, prev_price)
            
            # --- EVALUATE EXITS IF TRADED ---
            if coin in trades:
                t = trades[coin]
                elapsed = time.time() - t["time"]
                
                if t["direction"] == "LONG":
                    t["highest_price"] = max(t["highest_price"], price)
                    if price <= t["entry"] * (1 - (t["sl_pct"] / 100)):
                        close_trade(coin, price, "SL HIT")
                    elif price >= t["entry"] * (1 + (t["tp_pct"] / 100)):
                        close_trade(coin, price, "TP HIT")
                    elif elapsed > MAX_HOLD_TIME:
                        close_trade(coin, price, "TIME EXPIRED")
                else:
                    t["lowest_price"] = min(t["lowest_price"], price)
                    if price >= t["entry"] * (1 + (t["sl_pct"] / 100)):
                        close_trade(coin, price, "SL HIT")
                    elif price <= t["entry"] * (1 - (t["tp_pct"] / 100)):
                        close_trade(coin, price, "TP HIT")
                    elif elapsed > MAX_HOLD_TIME:
                        close_trade(coin, price, "TIME EXPIRED")
                continue
                
            # --- EVALUATE ENTRIES ---
            if score >= 6:
                open_trade(coin, "LONG", price, score, vol, rsi_val)
            elif score <= -6:
                open_trade(coin, "SHORT", price, score, vol, rsi_val)
                
        except Exception as e:
            print(f"Error checking strategy state for {coin}: {e}")

if __name__ == "__main__":
    print(f"[{timestamp()}] Starting Bot 2 execution container...")
    while True:
        run_scanner()
        # Empty background queue rows into spreadsheet sequentially 
        for _ in range(len(sheet_queue)):
            flush_sheet()
        time.sleep(CYCLE_SLEEP)



