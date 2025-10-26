import statistics
import requests
from datetime import datetime, timezone
from config import BINANCE_FUTURES_BASE

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "daily-gainer-bot/vC"})

SESSION.headers.update({"Cache-Control": "no-cache"})
EXCLUDE_KEYWORDS = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT", "BUSD")

def now_ts_ms():
    return int(datetime.now(timezone.utc).timestamp() * 1000)
# utils.py（任一合適位置）
def ws_best_price(symbol: str):
    try:
        from ws_client import ws_best_price as _ws
        return _ws(symbol)
    except Exception:
        return None
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


# 安裝全域重試（429/5xx，帶退避）
_retry = Retry(total=3, backoff_factor=0.4, status_forcelist=[429,500,502,503,504], allowed_methods=["GET","POST","DELETE"])
SESSION.mount("https://", HTTPAdapter(max_retries=_retry))
SESSION.mount("http://",  HTTPAdapter(max_retries=_retry))


def safe_get_json(url: str, params=None, timeout=4, tries=2):
    """帶重試/timeout 的 GET，失敗拋例外讓上層 decide。"""
    params = params or {}
    last = None
    for i in range(max(1, tries)):
        try:
            r = SESSION.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            time.sleep(0.3 * (i + 1))
    raise last
