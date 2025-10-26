from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import time, math
import statistics
import requests
from datetime import datetime, timezone
from config import BINANCE_FUTURES_BASE

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "daily-gainer-bot/vC"})

SESSION.headers.update({"Cache-Control": "no-cache"})
EXCLUDE_KEYWORDS = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT", "BUSD")
# 用於緩存所有幣種的精度規則
EXCHANGE_INFO = {}

def load_exchange_info():
    """
    在啟動時呼叫一次，獲取並緩存所有交易對的精度規則。
    """
    global EXCHANGE_INFO
    if EXCHANGE_INFO: return # 避免重複載入
    try:
        # 使用 _rest_json 來獲取 exchangeInfo
        info = _rest_json("/fapi/v1/exchangeInfo")
        data = {}
        for s in info.get("symbols", []):
            if s.get('status') != 'TRADING':
                continue
            data[s['symbol']] = {
                'pricePrecision': s.get('pricePrecision', 8),     # 價格小數位
                'quantityPrecision': s.get('quantityPrecision', 8), # 數量小數位
            }
        EXCHANGE_INFO = data
        print(f"--- 成功載入 {len(EXCHANGE_INFO)} 個幣種的精度規則 ---")
    except Exception as e:
        print(f"--- 致命錯誤：無法載入 Exchange Info: {e} ---")
        print("--- 程式可能因無法獲取精度而下單失敗 ---")
        # 在真實環境中，這裡應該 raise e 讓程式停止
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
    rows = _rest_json("/fapi/v1/ticker/24hr", timeout=6, tries=3)
    items = []
    for x in rows:
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
    rows = _rest_json("/fapi/v1/klines", params={"symbol":symbol, "interval":interval, "limit":limit}, timeout=6, tries=3)
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

# --- Resilient REST hosts (main & testnet) ---
FUTURES_HOSTS_MAIN  = ["https://fapi.binance.com", "https://fapi1.binance.com", "https://fapi2.binance.com"]
FUTURES_HOSTS_TEST  = ["https://testnet.binancefuture.com"]

def _rest_json(path: str, params=None, timeout=5, tries=3):
    """對 Binance Futures REST 做多主機輪詢 + 退避重試。
    不依賴全域 BASE，動態選 main/testnet，避免單一 host DNS 抽風。
    """
    from config import USE_TESTNET  # 延遲載入避免循環
    hosts = FUTURES_HOSTS_TEST if USE_TESTNET else FUTURES_HOSTS_MAIN
    params = params or {}
    last_err = None
    for t in range(max(1, tries)):
        for base in hosts:
            try:
                r = SESSION.get(f"{base}{path}", params=params, timeout=timeout)
                r.raise_for_status()
                return r.json()
            except Exception as e:
                last_err = e
        # 退避 + 一點 jitter
        time.sleep(0.3 * (t + 1))
    # 把最後一個錯誤丟回去，外層會捕捉並 log，不讓主迴圈掛掉
    raise last_err if last_err else RuntimeError("REST all hosts failed")


# --- Binance Futures server time offset (ms) ---
def _fapi_server_time_ms():
    try:
        import requests
        r = SESSION.get(f"{BINANCE_FUTURES_BASE}/fapi/v1/time", timeout=5)
        r.raise_for_status()
        return int(r.json().get("serverTime", now_ts_ms()))
    except Exception:
        return now_ts_ms()

try:
    _local = now_ts_ms()
    _server = _fapi_server_time_ms()
    TIME_OFFSET_MS = _server - _local
except Exception:
    TIME_OFFSET_MS = 0
