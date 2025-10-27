from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import time
import statistics
import requests
from datetime import datetime, timezone
import math
from config import BINANCE_FUTURES_BASE, BINANCE_FUTURES_TEST_BASE, USE_TESTNET, SYMBOL_BLACKLIST
from typing import List, Optional # <-- 新增

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "daily-gainer-bot/vC"})
SESSION.headers.update({"Cache-Control": "no-cache"})
EXCLUDE_KEYWORDS = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT", "BUSD")

# --- 全域變數 ---
TIME_OFFSET_MS = 0 # 時間偏移
EXCHANGE_INFO = {} # 精度規則

def now_ts_ms():
    return int(datetime.now(timezone.utc).timestamp() * 1000)

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
        # --- 檢查黑名單 ---
        if s in SYMBOL_BLACKLIST:
            continue
        # --- 結束 ---
        try:
            pct = float(x.get("priceChangePercent", 0.0))
            last = float(x.get("lastPrice", 0.0))
            vol  = float(x.get("volume", 0.0)) # 這裡是幣的數量
            quote_vol = float(x.get("quoteVolume", 0.0)) # 這裡是 USDT 成交額
        except:
            continue
        if last <= 0 or quote_vol <= 0: # 使用 quoteVolume 過濾較好
            continue
        items.append((s, pct, last, quote_vol)) # 回傳 quote_vol
    items.sort(key=lambda t: t[1], reverse=True)
    return items[:limit]

def fetch_klines(symbol, interval, limit):
    rows = _rest_json("/fapi/v1/klines", params={"symbol":symbol, "interval":interval, "limit":limit}, timeout=6, tries=3)
    closes = [float(k[4]) for k in rows]
    highs  = [float(k[2]) for k in rows]
    lows   = [float(k[3]) for k in rows] # <-- 新增
    vols   = [float(k[5]) for k in rows] # 這裡是幣的數量
    return closes, highs, lows, vols # <-- 修改

def ema(vals, n):
    if len(vals) < n: return None
    k = 2.0/(n+1.0)
    e = vals[0]
    for v in vals[1:]:
        e = v*k + e*(1-k)
    return e

# --- ATR 計算 ---
def calculate_atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> Optional[float]:
    """計算 Average True Range (ATR)"""
    if len(closes) < period + 1:
        return None # 數據不足

    true_ranges = []
    for i in range(1, len(closes)):
        high = highs[i]
        low = lows[i]
        prev_close = closes[i-1]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)

    # 使用指數移動平均 (EMA / Wilder's smoothing) 計算 ATR
    atr = ema(true_ranges, period) # 使用 ema 函數
    return atr

# 安裝全域重試（429/5xx，帶退避）
_retry = Retry(total=3, backoff_factor=0.4, status_forcelist=[429,500,502,503,504], allowed_methods=["GET","POST","DELETE"])
SESSION.mount("https://", HTTPAdapter(max_retries=_retry))
SESSION.mount("http://",  HTTPAdapter(max_retries=_retry))

FUTURES_HOSTS_MAIN  = ["https://fapi.binance.com", "https://fapi1.binance.com", "https://fapi2.binance.com"]
FUTURES_HOSTS_TEST  = ["https://testnet.binancefuture.com"]

def _rest_json(path: str, params=None, timeout=5, tries=3):
    """對 Binance Futures REST 做多主機輪詢 + 退避重試。"""
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
        time.sleep(0.3 * (t + 1))
    raise last_err if last_err else RuntimeError("REST all hosts failed")

# --- Binance Futures server time offset (ms) ---
def _fapi_server_time_ms():
    try:
        base = BINANCE_FUTURES_TEST_BASE if USE_TESTNET else BINANCE_FUTURES_BASE
        r = SESSION.get(f"{base}/fapi/v1/time", timeout=5)
        r.raise_for_status()
        return int(r.json().get("serverTime", now_ts_ms()))
    except Exception:
        return now_ts_ms()

def update_time_offset():
    """重新計算並更新全域的 TIME_OFFSET_MS"""
    global TIME_OFFSET_MS
    try:
        _local = now_ts_ms()
        _server = _fapi_server_time_ms()
        TIME_OFFSET_MS = _server - _local
        return TIME_OFFSET_MS
    except Exception:
        return TIME_OFFSET_MS # 同步失敗時，維持舊的 offset

# --- Exchange Info (精度規則) ---
def load_exchange_info():
    """
    在啟動時呼叫一次，獲取並緩存所有交易對的精度規則。
    (main.py 會在 KeyError 時重新呼叫此函數)
    """
    global EXCHANGE_INFO
    try:
        info = _rest_json("/fapi/v1/exchangeInfo")
        data = {}
        for s in info.get("symbols", []):
            if s.get('contractType') == 'PERPETUAL' and s.get('status') == 'TRADING':
                data[s['symbol']] = {
                    'pricePrecision': s.get('pricePrecision', 8),
                    'quantityPrecision': s.get('quantityPrecision', 8),
                }
        EXCHANGE_INFO = data # 原子化更新
        print(f"--- Successfully loaded/refreshed {len(EXCHANGE_INFO)} symbol precisions ---")
    except Exception as e:
        print(f"--- FATAL: Failed to load/refresh Exchange Info: {e} ---")
        print("--- Bot may fail placing orders due to unknown precision ---")

# --- 啟動時執行 ---
update_time_offset()
