import os
import time
import json
import requests
import gspread
import numpy as np
import pandas as pd
from collections import defaultdict
from datetime import datetime
from google.oauth2.service_account import Credentials

# =========================================================
# CONFIG
# =========================================================

COINS = [
    "ZEC-USDT", "BNB-USDT", "AVAX-USDT", "LINK-USDT",
    "SUI-USDT", "HYPE-USDT", "TAO-USDT", "ONDO-USDT",
    "VVV-USDT", "AR-USDT", "ICP-USDT", "NEAR-USDT",
    "BTC-USDT", "ETH-USDT", "SOL-USDT"
]

CYCLE_SLEEP = 300
MAX_OPEN_TRADES = 3
ENTRY_COOLDOWN = 1800

trades = {}
last_entry = defaultdict(float)
price_cache = {}

# =========================================================
# TELEGRAM
# =========================================================

TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

def send_telegram(msg):
    try:
        if TOKEN and CHAT_ID:
            requests.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": msg},
                timeout=10
            )
    except Exception as e:
        print("Telegram error:", e)

# =========================================================
# GOOGLE SHEETS
# =========================================================

sheet = None

def log_sheet(row):
    global sheet
    if sheet:
        try:
            sheet.append_row(row)
        except Exception as e:
            print("Sheet error:", e)

try:
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")

    if creds_json:
        creds = Credentials.from_service_account_info(
            json.loads(creds_json),
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )

        sheet = gspread.authorize(creds)\
            .open_by_key("1uJPJ_CFBW_qU9mpqoHVS3oAgPH3Cg6ZzATFv1zd4S64")\
            .sheet1

        print("Sheets connected")
    else:
        print("Sheets disabled")

except Exception as e:
    print("Sheets init error:", e)
    sheet = None

# =========================================================
# DATA FEED (KUCOIN)
# =========================================================

def fetch(symbol):
    try:
        url = f"https://api.kucoin.com/api/v1/market/candles?type=5min&symbol={symbol}"
        r = requests.get(url, timeout=10).json()

        data = r.get("data", [])
        closes = []

        for c in data[:120]:
            try:
                closes.append(float(c[2]))
            except:
                continue

        closes.reverse()

        if len(closes) < 50:
            return None

        price_cache[symbol] = closes
        return closes

    except:
        return price_cache.get(symbol)

# =========================================================
# INDICATORS
# =========================================================

def ema(data, period):
    s = pd.Series(data)
    return s.ewm(span=period, adjust=False).mean().iloc[-1]

def rsi(data):
    diff = np.diff(data)
    gain = np.mean(np.clip(diff, 0, None))
    loss = np.mean(np.clip(-diff, 0, None))
    if loss == 0:
        return 100
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def volatility(data):
    return np.std(data[-30:]) / np.mean(data[-30:])

# =========================================================
# SCORE ENGINE
# =========================================================

def calculate_score(e21, e50, rsi_val, vol, price, prev_price):

    if e21 > e50:
        score = 3
        if rsi_val > 55:
            score += 2
        elif rsi_val < 45:
            score += 1
    else:
        score = -3
        if rsi_val < 45:
            score -= 2
        elif rsi_val > 55:
            score -= 1

    if price > prev_price:
        score += 1
    else:
        score -= 1

    if vol > 0.002:
        score += 1 if score > 0 else -1

    return round(score, 2)

# =========================================================
# RISK
# =========================================================

def calc_sl_tp(direction, price, vol):

    sl_pct = 1.5 * max(1, vol / 0.0025)
    tp_pct = 3.0 * max(1, vol / 0.0025)

    if direction == "LONG":
        sl = price * (1 - sl_pct / 100)
        tp = price * (1 + tp_pct / 100)
    else:
        sl = price * (1 + sl_pct / 100)
        tp = price * (1 - tp_pct / 100)

    return sl, tp

# =========================================================
# TRADE ENGINE
# =========================================================

def open_trade(symbol, direction, price, score, rsi_val, vol):

    if len(trades) >= MAX_OPEN_TRADES:
        return

    if time.time() - last_entry[symbol] < ENTRY_COOLDOWN:
        return

    sl, tp = calc_sl_tp(direction, price, vol)

    trades[symbol] = {
        "direction": direction,
        "entry": price,
        "sl": sl,
        "tp": tp,
        "score": score,
        "time": time.time()
    }

    last_entry[symbol] = time.time()

    send_telegram(f"""
🚨 BOT 2 TRADE
{symbol} {direction}
Price: {price}
Score: {score}
SL: {sl:.4f}
TP: {tp:.4f}
""")

    log_sheet([
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "BOT 2",
        symbol,
        direction,
        score,
        "OPEN",
        price,
        sl,
        tp,
        rsi_val,
        vol
    ])

# =========================================================
# CLOSE TRADE
# =========================================================

def close_trade(symbol, price, reason):

    t = trades[symbol]
    entry = t["entry"]
    direction = t["direction"]

    pnl = ((price - entry) / entry) * 100
    if direction == "SHORT":
        pnl = -pnl

    send_telegram(f"""
🚨 BOT 2 CLOSED
{symbol}
Reason: {reason}
PnL: {pnl:.2f}%
""")

    log_sheet([
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "BOT 2",
        symbol,
        direction,
        t["score"],
        reason,
        entry,
        price,
        pnl,
        "",
        ""
    ])

    del trades[symbol]

# =========================================================
# MANAGE TRADE
# =========================================================

def manage_trade(symbol, price):

    if symbol not in trades:
        return

    t = trades[symbol]

    pnl = ((price - t["entry"]) / t["entry"]) * 100
    if t["direction"] == "SHORT":
        pnl = -pnl

    if pnl >= 3:
        close_trade(symbol, price, "TP")

    if pnl <= -1.5:
        close_trade(symbol, price, "SL")

# =========================================================
# MAIN LOOP
# =========================================================

if __name__ == "__main__":

    send_telegram("🚀 BOT 2 ONLINE")

    while True:

        try:
            print("\nCycle Start", datetime.now())

            for coin in COINS:

                closes = fetch(coin)
                if not closes:
                    continue

                price = closes[-1]
                prev = closes[-2]

                if coin in trades:
                    manage_trade(coin, price)
                    continue

                e21 = ema(closes, 21)
                e50 = ema(closes, 50)
                rsi_val = rsi(closes)
                vol = volatility(closes)

                score = calculate_score(e21, e50, rsi_val, vol, price, prev)

                if score >= 8.5:
                    open_trade(coin, "LONG", price, score, rsi_val, vol)

                elif score <= -8.5:
                    open_trade(coin, "SHORT", price, score, rsi_val, vol)

            time.sleep(CYCLE_SLEEP)

        except Exception as e:
            print("MAIN ERROR:", e)
            time.sleep(10)
