import statistics
import requests
from datetime import datetime, timezone
from config import BINANCE_FUTURES_BASE

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "daily-gainer-bot/vC"})

EXCLUDE_KEYWORDS = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT", "BUSD")

def now_ts_ms():
    return int(datetime.now(timezone.utc).timestamp() * 1000)

def fetch_top_gainers(limit=10):
    r = SESSION.get(f"{BINANCE_FUTURES_BASE}/fapi/v1/ticker/24hr", timeout=10)
    r.raise_for_status()
    items = []
    for x in r.json():
        s = x.get("symbol", "")
        if (not s.endswith("USDT")) or any(k in s for k in EXCLUDE_KEYWORDS):
            continue
        try:
            pct = float(x.get("priceChangePercent", 0.0))
            last = float(x.get("lastPrice", 0.0))
            vol  = float(x.get("volume", 0.0))
        except:
            continue
        if last <= 0 or vol <= 0:
            continue
        items.append((s, pct, last, vol))
    items.sort(key=lambda t: t[1], reverse=True)
    return items[:limit]

def fetch_klines(symbol, interval, limit):
    r = SESSION.get(f"{BINANCE_FUTURES_BASE}/fapi/v1/klines",
                    params={"symbol":symbol, "interval":interval, "limit":limit},
                    timeout=10)
    r.raise_for_status()
    rows = r.json()
    closes = [float(k[4]) for k in rows]
    highs  = [float(k[2]) for k in rows]
    vols   = [float(k[5]) for k in rows]
    return closes, highs, vols

def ema(vals, n):
    if len(vals) < n: return None
    k = 2.0/(n+1.0)
    e = vals[0]
    for v in vals[1:]:
        e = v*k + e*(1-k)
    return e