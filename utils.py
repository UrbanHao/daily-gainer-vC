from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import time,random
import statistics
import requests
from datetime import datetime, timezone
import math, random
# 移除 MIN_NOTIONAL_FALLBACK 的 import，改從 config 讀
from config import BINANCE_FUTURES_BASE, BINANCE_FUTURES_TEST_BASE, USE_TESTNET, SYMBOL_BLACKLIST, MIN_NOTIONAL_FALLBACK
from typing import List, Optional
from decimal import Decimal, ROUND_DOWN, ROUND_UP, InvalidOperation # <-- 新增 Decimal

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "daily-gainer-bot/vC"})
SESSION.headers.update({"Cache-Control": "no-cache"})
EXCLUDE_KEYWORDS = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT", "BUSD")

# --- Binance REST endpoints（期貨 FAPI） ---
_BINANCE_FAPI_BASES = [
    "https://fapi1.binance.com",
    "https://fapi2.binance.com",
    # 預備：主域名也可做 fallback（有些網路環境對 fapi1/2 不友善）
    "https://fapi.binance.com",
]

# 供 round-robin 取用
__ep_idx = 0

def _choose_endpoint(path: str) -> str:
    """
    回傳帶 base 的完整 URL。
    與舊版相容：允許傳入以 '/' 開頭的 path。
    """
    global __ep_idx
    base = _BINANCE_FAPI_BASES[__ep_idx % len(_BINANCE_FAPI_BASES)]
    __ep_idx += 1
    if path.startswith("/"):
        return base + path
    return f"{base}/{path}"

def _request_json(session, method: str, path: str, params=None, max_retry: int = 3, timeout: float = 10.0):
    """
    小型請求器：處理 202/429/5xx，自動換 endpoint 重試。
    與你現有呼叫點相容：用 path 丟進來即可（例如 '/fapi/v1/ticker/24hr'）。
    """
    for attempt in range(max_retry):
        url = _choose_endpoint(path)
        try:
            r = session.request(method, url, params=params, timeout=timeout)
        except Exception:
            # 網路錯誤 -> 短暫睡一下再換下一個 endpoint
            time.sleep(0.2 + 0.3 * attempt)
            continue

        # 正常
        if r.status_code == 200:
            return r.json()

        # 常見暫時性狀態：202/429/5xx -> 換 endpoint 重試
        if r.status_code in (202, 429) or 500 <= r.status_code < 600:
            print(f"Warning: Received {r.status_code} from {url}. Treating as temporary failure, retrying...")
            time.sleep(0.2 + 0.3 * attempt)
            continue

        # 其他狀態碼（例如 4xx 非 429）直接 raise
        try:
            r.raise_for_status()
        except Exception as e:
            raise e

    # 超過重試次數
    raise RuntimeError(f"Request failed after {max_retry} retries for path={path}")
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
        if s in SYMBOL_BLACKLIST:
            continue
        try:
            pct = float(x.get("priceChangePercent", 0.0))
            last = float(x.get("lastPrice", 0.0))
            quote_vol = float(x.get("quoteVolume", 0.0))
        except (ValueError, TypeError):
            continue
        if last <= 0 or quote_vol <= 0:
            continue
        items.append((s, pct, last, quote_vol))
    items.sort(key=lambda t: t[1], reverse=True)
    return items[:limit]
def fetch_top_losers(n: int = 10):
    """
    跌幅榜（和 fetch_top_gainers 結構一致）：
    回傳 [(symbol, priceChangePercent, lastPrice, quoteVolume), ...] 取前 n 名。
    """
    try:
        # ✅ 統一走 _rest_json（內含多主機輪詢 + 202/429/5xx 處理）
        data = _rest_json("/fapi/v1/ticker/24hr", timeout=6, tries=3)

        rows = []
        for item in data:
            s = item.get("symbol")
            if not s or not s.endswith("USDT"):
                continue
            if s in SYMBOL_BLACKLIST:
                continue
            try:
                pct  = float(item.get("priceChangePercent", 0.0))
                last = float(item.get("lastPrice", 0.0))
                vol  = float(item.get("quoteVolume", 0.0))
            except Exception:
                continue
            # 僅保留有效數據
            if last <= 0 or vol <= 0:
                continue
            rows.append((s, pct, last, vol))

        # 依跌幅排序（最負在最前面）
        rows.sort(key=lambda x: x[1])
        return rows[:n]

    except Exception as e:
        print(f"Warning(fetch_top_losers): {e}")
        return []

# 放在檔案頂部已有 import 後面（若無 random 請加上）
import time, random

# 簡單記憶體快取：（symbol, interval） -> { "ts": 上次成功時間, "data": (closes, highs, lows, vols) }
_KLINE_CACHE = {}
_KLINE_CACHE_TTL = 20.0  # 成功資料可沿用 20 秒，避免 202 時瘋狂打 API

def fetch_klines(symbol, interval, limit):
    """
    回傳四個 list: closes, highs, lows, vols
    - 先看快取（20 秒內）直接回
    - 若 API 回 202/429/5xx，最多重試 2 次；若仍失敗但有快取 → 回快取
      沒快取才 raise 讓呼叫端略過該檔
    """
    cache_key = (symbol, interval)
    now = time.time()

    # 1) 有新鮮快取 → 直接回
    if cache_key in _KLINE_CACHE:
        c = _KLINE_CACHE[cache_key]
        if now - c["ts"] <= _KLINE_CACHE_TTL:
            return c["data"]

    # 2) 打公開 REST（用你現成的 _rest_json；限制 tries=2；加一點隨機抖動）
    #    注意：_rest_json 的 path 只放 path，底層會自己輪詢 base host
    params = {"symbol": symbol, "interval": interval, "limit": int(limit)}
    # 小抖動，避免同時間全打到同一主機
    time.sleep(0.05 + random.random() * 0.05)

    data = None
    try:
        data = _rest_json("/fapi/v1/klines", params=params, timeout=5, tries=2)
    except Exception:
        # 若 API 失敗但有舊快取，回舊快取；否則往上拋
        if cache_key in _KLINE_CACHE:
            return _KLINE_CACHE[cache_key]["data"]
        raise

    # 3) 解析成四個 list（保留你原本四清單約定）
    try:
        closes = [float(x[4]) for x in data]
        highs  = [float(x[2]) for x in data]
        lows   = [float(x[3]) for x in data]
        vols   = [float(x[5]) for x in data]
    except Exception as e:
        # 解析失敗：若有舊快取則回舊快取；否則拋出
        if cache_key in _KLINE_CACHE:
            return _KLINE_CACHE[cache_key]["data"]
        raise e

    out = (closes, highs, lows, vols)

    # 4) 寫入快取
    _KLINE_CACHE[cache_key] = {"ts": now, "data": out}
    return out


def ema(vals, n):
    if not vals or len(vals) < n or n <= 0: return None
    try:
        k = Decimal(2) / (Decimal(n) + Decimal(1))
        e = to_decimal(vals[0])
        if not e.is_finite(): # Handle potential NaN from to_decimal
             # Find first finite value to start
             for val_start in vals:
                 e_start = to_decimal(val_start)
                 if e_start.is_finite():
                     e = e_start
                     break
             if not e.is_finite(): return None # Cannot start EMA

        for v_str in vals[1:]:
             v = to_decimal(v_str)
             if v.is_finite(): # Only update EMA with valid numbers
                 e = v * k + e * (Decimal(1) - k)
        return float(e) if e.is_finite() else None
    except (InvalidOperation, TypeError, IndexError):
        return None # Return None if calculation fails


# --- ATR 計算 ---
def calculate_atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> Optional[float]:
    """計算 Average True Range (ATR)"""
    if len(closes) < period + 1 or period <= 0:
        return None # 數據不足或週期無效

    true_ranges = []
    for i in range(1, len(closes)):
        try:
            high = highs[i]
            low = lows[i]
            prev_close = closes[i-1]
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            true_ranges.append(tr)
        except (IndexError, TypeError, ValueError):
             continue # Skip if data is bad

    # 需要至少 period 個 TR 值才能計算 EMA
    if len(true_ranges) < period:
        return None

    # 使用指數移動平均 (EMA / Wilder's smoothing) 計算 ATR
    atr_float = ema(true_ranges, period) # 使用 ema 函數 (它內部用 Decimal)
    return atr_float if atr_float is not None and atr_float > 0 else None # 確保回傳有效 ATR


# --- Decimal 精度輔助函數 ---
def to_decimal(value) -> Decimal:
    """將數字或字串安全轉換為 Decimal"""
    try:
        # Handle potential scientific notation strings
        if isinstance(value, str) and ('e' in value or 'E' in value):
             dec_val = Decimal(value)
        else:
             dec_val = Decimal(str(value))
        # Check for NaN or infinity which Decimal handles
        if not dec_val.is_finite():
             return Decimal('NaN')
        return dec_val
    except (InvalidOperation, ValueError, TypeError):
        return Decimal('NaN') # Return NaN on failure


def floor_step_decimal(value: Decimal, step: Decimal) -> Decimal:
    """使用 Decimal 向下對齊步長 (適用數量)"""
    # 增加檢查 is_finite 和 step > 0
    if not value.is_finite() or not step.is_finite() or step <= Decimal(0):
        # print(f"DEBUG floor_step: Invalid input value={value} step={step}") # Debug
        return Decimal('NaN')
    try:
        # quantize(Decimal('1')) effectively takes the integer part
        # ROUND_DOWN ensures we always move towards negative infinity (floor)
        quantized_steps = (value / step).quantize(Decimal('1'), rounding=ROUND_DOWN)
        result = quantized_steps * step
        # print(f"DEBUG floor_step: value={value}, step={step}, result={result}") # Debug
        return result
    except (InvalidOperation, TypeError):
        # print(f"DEBUG floor_step: Exception during calculation value={value} step={step}") # Debug
        return Decimal('NaN')


def round_tick_decimal(value: Decimal, tick: Decimal, direction: int = 0) -> Decimal:
    """
    使用 Decimal 對齊價格 tick。
    direction: 0=四捨五入(預設, round half up), +1=向上(ceil), -1=向下(floor)
    """
    if not value.is_finite() or not tick.is_finite() or tick <= Decimal(0):
        # print(f"DEBUG round_tick: Invalid input value={value} tick={tick}") # Debug
        return Decimal('NaN')
    try:
        # Calculate number of steps (potentially fractional)
        steps_exact = value / tick
        # Determine rounding based on direction
        if direction > 0: # Ceil
            steps_quantized = steps_exact.quantize(Decimal('1'), rounding=ROUND_UP)
        elif direction < 0: # Floor
            steps_quantized = steps_exact.quantize(Decimal('1'), rounding=ROUND_DOWN)
        else: # Round half up (Python's default Decimal rounding might differ slightly, emulate manually if needed)
            # Standard quantize might round half to even, let's use ROUND_HALF_UP explicitly if needed
            # For simplicity matching typical trading rounding, we can use ROUND_HALF_UP if available or fallback
            from decimal import ROUND_HALF_UP
            steps_quantized = steps_exact.quantize(Decimal('1'), rounding=ROUND_HALF_UP)

        result = steps_quantized * tick
        # print(f"DEBUG round_tick: value={value}, tick={tick}, dir={direction}, result={result}") # Debug
        return result
    except (InvalidOperation, TypeError):
        # print(f"DEBUG round_tick: Exception value={value} tick={tick} dir={direction}") # Debug
        return Decimal('NaN')


# 安裝全域重試（429/5xx，帶退避）
_retry = Retry(total=3, backoff_factor=0.4, status_forcelist=[429,500,502,503,504], allowed_methods=["GET","POST","DELETE"])
SESSION.mount("https://", HTTPAdapter(max_retries=_retry))
SESSION.mount("http://",  HTTPAdapter(max_retries=_retry))

FUTURES_HOSTS_MAIN  = ["https://fapi.binance.com", "https://fapi1.binance.com", "https://fapi2.binance.com"]
FUTURES_HOSTS_TEST  = ["https://testnet.binancefuture.com"]

def _rest_json(path: str, params=None, timeout=5, tries=3):
    """對 Binance Futures REST 做多主機輪詢 + 退避重試 (優化版)。"""
    hosts = FUTURES_HOSTS_TEST if USE_TESTNET else FUTURES_HOSTS_MAIN
    params = params or {}
    last_err = None
    for t in range(max(1, tries)):
        # 嘗試隨機選擇主機，增加成功率
        shuffled_hosts = random.sample(hosts, len(hosts))
        for base in shuffled_hosts:
            try:
                if not base.startswith(("http://", "https://")):
                     raise ValueError(f"Invalid base URL: {base}")

                r = SESSION.get(f"{base}{path}", params=params, timeout=timeout)

                # --- 核心修改：明確處理 202 ---
                if r.status_code == 202:
                    print(f"Warning: Received 202 Accepted from {base}{path}. Treating as temporary failure, retrying...")
                    last_err = requests.exceptions.HTTPError(f"202 Accepted (non-final)", response=r)
                    continue # 立刻嘗試下一個主機/重試

                # --- 處理速率限制 (418/429) ---
                if r.status_code == 418 or r.status_code == 429:
                     print(f"Rate limit hit ({r.status_code}) on {base}, retrying...")
                     last_err = requests.exceptions.HTTPError(f"{r.status_code} Rate Limit", response=r)
                     # 短暫等待後再嘗試下一個主機，讓 Retry 機制處理更長的退避
                     time.sleep(0.1 + random.uniform(0, 0.2))
                     continue

                # --- 檢查其他錯誤 ---
                r.raise_for_status()

                # --- 檢查是否為 JSON ---
                if 'application/json' in r.headers.get('Content-Type', '').lower():
                    try:
                        return r.json()
                    except requests.exceptions.JSONDecodeError as json_e:
                        print(f"Error: Failed to decode JSON from {base}{path}. Status: {r.status_code}. Error: {json_e}")
                        last_err = json_e
                        continue # JSON 解析失敗，嘗試下一個
                else:
                    # 收到非 JSON 成功回應
                    print(f"Warning: Non-JSON response received from {base}{path}. Status: {r.status_code}, Content: {r.text[:100]}")
                    last_err = ValueError(f"Non-JSON response received: {r.status_code}")
                    continue # 視為失敗，嘗試下一個

            except requests.exceptions.Timeout:
                last_err = requests.exceptions.Timeout(f"Timeout contacting {base}")
                print(f"Warning: Timeout contacting {base}{path}")
                # 超時後直接嘗試下一個主機
            except requests.exceptions.RequestException as e:
                last_err = e
                print(f"Warning: Network error contacting {base}: {e}")
                # 網路錯誤後稍微等待
                time.sleep(0.1 + random.uniform(0, 0.1))
            except Exception as e: # 捕捉其他意外錯誤
                last_err = e
                print(f"Unexpected error with {base}: {type(e).__name__}: {e}")
                time.sleep(0.1) # 意外錯誤後短暫等待

        # 如果所有主機在本輪都失敗了，等待一段時間再重試
        if t < tries - 1:
            wait_time = 0.5 * (t + 1) # 增加基礎等待時間
            print(f"All hosts failed on attempt {t+1}/{tries}, waiting {wait_time:.1f}s before retrying {path}...")
            time.sleep(wait_time)

    # 所有重試都失敗後
    print(f"--- CRITICAL: REST request failed permanently after {tries} tries: {path} ---")
    raise last_err if last_err else RuntimeError(f"REST request failed after {tries} tries: {path}")

# --- Binance Futures server time offset (ms) ---
def _fapi_server_time_ms():
    # Uses _rest_json which includes retry logic
    try:
        # Use a shorter timeout as time sync should be fast
        time_data = _rest_json("/fapi/v1/time", timeout=2, tries=2)
        return int(time_data.get("serverTime", now_ts_ms()))
    except Exception as e:
        print(f"Warning: Failed to get server time, using local time. Error: {e}")
        return now_ts_ms()

def update_time_offset():
    """重新計算並更新全域的 TIME_OFFSET_MS"""
    global TIME_OFFSET_MS
    try:
        _local = now_ts_ms()
        _server = _fapi_server_time_ms()
        new_offset = _server - _local
        # Add a sanity check for large offsets
        if abs(new_offset) > 60000: # Offset > 60 seconds is suspicious
             print(f"Warning: Large time offset detected: {new_offset}ms. Check system clock synchronization (NTP).")
             # Optionally, revert to 0 or keep previous offset if deemed safer
             # TIME_OFFSET_MS = 0
        else:
             TIME_OFFSET_MS = new_offset
        # print(f"DEBUG: Time offset updated to {TIME_OFFSET_MS} ms") # Debug
        return TIME_OFFSET_MS
    except Exception as e:
        print(f"Warning: Failed to update time offset. Error: {e}")
        return TIME_OFFSET_MS # 同步失敗時，維持舊的 offset

# --- Exchange Info (精度規則) ---
def load_exchange_info(force_refresh: bool = False, *_, **__):
    """
    獲取並緩存所有交易對的精度規則。
    (main.py 會在 KeyError 時重新呼叫此函數)
    """
    global EXCHANGE_INFO
    try:
        print("Attempting to load/refresh exchange info...") # Debug print
        info = _rest_json("/fapi/v1/exchangeInfo")
        new_data = {}
        processed_count = 0
        skipped_count = 0
        for s_info in info.get("symbols", []):
            symbol = s_info.get("symbol")
            # 增加更嚴格的檢查
            if (symbol and
                s_info.get('contractType') == 'PERPETUAL' and
                s_info.get('status') == 'TRADING' and
                s_info.get('quoteAsset') == 'USDT' and # Ensure it's USDT-M
                s_info.get('maintMarginPercent') is not None): # Check for a valid margin percent as proxy for tradeable future

                min_notional_dec = None
                tick_size_str = None
                step_size_str = None
                price_prec = 8 # Default precision
                qty_prec = 8 # Default precision

                try:
                    price_prec = int(s_info.get('pricePrecision', 8))
                    qty_prec = int(s_info.get('quantityPrecision', 8))

                    for f in s_info.get("filters", []):
                        ftype = f.get("filterType")
                        if ftype == "PRICE_FILTER":
                            tick_size_str = f.get("tickSize")
                        elif ftype == "LOT_SIZE":
                            step_size_str = f.get("stepSize")
                        elif ftype == "MIN_NOTIONAL":
                             min_notional_str = f.get("notional")
                             if min_notional_str: min_notional_dec = to_decimal(min_notional_str)
                    
                    # Ensure essential rules were found and parsed
                    if tick_size_str is None or step_size_str is None:
                         print(f"Warning: Missing tickSize or stepSize for {symbol}. Skipping.")
                         skipped_count += 1
                         continue
                    
                    # Store rules (keep original precision ints, but add Decimal versions for calculation)
                    new_data[symbol] = {
                        'pricePrecision': price_prec,
                        'quantityPrecision': qty_prec,
                        'tickSize': tick_size_str, # Store as string
                        'stepSize': step_size_str, # Store as string
                        'minNotional': min_notional_dec, # Store as Decimal or None
                    }
                    processed_count += 1
                except (ValueError, TypeError, InvalidOperation) as e:
                    print(f"Warning: Error parsing rules for {symbol}: {e}. Skipping.")
                    skipped_count += 1
                    continue
            else:
                 skipped_count += 1
                 # Optional: Log skipped symbols if needed for debugging
                 # if symbol: print(f"DEBUG: Skipping symbol {symbol} due to status/type mismatch.")

        # Atomic update of the global cache
        EXCHANGE_INFO = new_data
        print(f"--- Successfully loaded/refreshed {processed_count} symbol precisions ({skipped_count} skipped) ---")
    except Exception as e:
        # Make the error message more prominent
        print(f"--- FATAL: Failed to load/refresh Exchange Info: {type(e).__name__}: {e} ---")
        print("--- Bot will likely fail placing orders due to unknown precision rules! ---")
        # Consider if bot should exit here if EXCHANGE_INFO is empty or loading failed critically
        if not EXCHANGE_INFO: # If cache is still empty after failure
             print("--- CRITICAL: EXCHANGE_INFO is empty. Bot cannot function reliably. Exiting. ---")
             # import sys
             # sys.exit(1) # Uncomment to force exit on critical failure


# === Helper to get rule safely ===
def get_symbol_rule(symbol: str, key: str, default=None):
    """安全地從 EXCHANGE_INFO 取得規則"""
    try:
        # Uppercase symbol for consistent lookup
        rule = EXCHANGE_INFO[symbol.upper()].get(key)
        # Handle case where minNotional might be None explicitly stored
        if rule is not None:
            # Special handling for Decimal types if needed by caller
             if key in ['minNotional'] and rule == Decimal('NaN'): return default # Treat stored NaN as missing
             # Convert numeric strings on demand? Or store Decimals directly?
             # For now, return as stored (str or Decimal or None)
             return rule
        else:
            # Key exists but value is None (like potentially minNotional)
            return default if default is not None else rule # Return None if default is None too
    except KeyError:
        # Symbol not in cache at all
        return default

# --- Initialize ---
update_time_offset()
# load_exchange_info() # Removed: main.py calls it initially
