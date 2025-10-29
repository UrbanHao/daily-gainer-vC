import statistics
from config import (KLINE_INTERVAL, KLINE_LIMIT, HH_N, OVEREXTEND_CAP,
                    VOL_BASE_WIN, VOL_SPIKE_K, VOL_LOOKBACK_CONFIRM,
                    EMA_FAST, EMA_SLOW, ATR_PERIOD,VBO_LOOKBACK_BARS, VBO_VOL_MULT, VBO_PCT_BREAK)

# --- 移除 fetch_klines 的 import ---
from utils import ema, calculate_atr # 只 import 需要的計算函數
from typing import Tuple, Optional, List # <-- 加入 List

# --- 修改：訊號函數接收 K 線數據 ---
def calculate_vbo_long_signal(closes: List[float], highs: List[float], lows: List[float], vols: List[float]) -> Tuple[bool, Optional[float]]:
    """ (修改) 根據傳入的 K 線數據計算多頭訊號和 ATR """
    try:
        # --- 使用傳入的數據長度進行檢查 ---
        required_len = max(HH_N, VOL_BASE_WIN, ATR_PERIOD) + VOL_LOOKBACK_CONFIRM + 2
        # 確保所有列表都足夠長
        if not all(len(lst) >= required_len for lst in [closes, highs, lows, vols]):
            # print(f"DEBUG: Data length insufficient for long signal. Need {required_len}, Got closes={len(closes)}") # Debug
            return False, None # 數據不足

        # --- VBO Breakout Logic (使用傳入數據) ---
        curr_close = closes[-1]
        # 確保索引有效
        prev_high_slice_start = -(HH_N + 1)
        if HH_N <= 0 or abs(prev_high_slice_start) >= len(highs): # Handle HH_N=0 or not enough data
             if len(highs) >= 2:
                 prev_high = highs[-2]
             else: return False, None # Not enough data for prev_high
        else:
            prev_high_slice = highs[prev_high_slice_start:-1]
            if not prev_high_slice: return False, None # Slice is empty
            prev_high = max(prev_high_slice)

        if curr_close <= prev_high: return False, None
        breakout_ratio = (curr_close - prev_high) / prev_high if prev_high > 0 else 0
        if breakout_ratio > OVEREXTEND_CAP: return False, None

        # --- Volume Logic (使用傳入數據) ---
        confirm_bars = VOL_LOOKBACK_CONFIRM if VOL_LOOKBACK_CONFIRM > 0 else 1 # Min 1 bar
        base_window_len = VOL_BASE_WIN
        total_vol_len_needed = base_window_len + confirm_bars
        if len(vols) < total_vol_len_needed: return False, None # Check length before slicing

        base_window_end_idx = -confirm_bars
        base_window_start_idx = base_window_end_idx - base_window_len

        # Check indices again just to be safe
        if base_window_start_idx < -len(vols) or base_window_end_idx >= 0:
             # print(f"DEBUG: Invalid volume window indices. Start={base_window_start_idx}, End={base_window_end_idx}, Len={len(vols)}") # Debug
             return False, None

        base_window = vols[base_window_start_idx:base_window_end_idx]
        if not base_window: return False, None

        try:
             base_med = statistics.median(base_window)
        except statistics.StatisticsError:
             # print(f"DEBUG: StatisticsError calculating median for base_window (len={len(base_window)})") # Debug
             return False, None

        recent_slice_start = -confirm_bars
        # Check index
        if abs(recent_slice_start) > len(vols): return False, None
        recent_vols = vols[recent_slice_start:]
        if not recent_vols: return False, None

        recent_sum = sum(recent_vols)
        need_sum = VOL_SPIKE_K * base_med * confirm_bars # Use confirm_bars count
        if recent_sum < need_sum: return False, None

        # --- EMA Filter (使用傳入數據) ---
        required_ema_len = EMA_SLOW + 10
        if len(closes) < required_ema_len: return False, None
        segment = closes[-required_ema_len:]
        e_fast = ema(segment, EMA_FAST)
        e_slow = ema(segment, EMA_SLOW)
        if e_fast is None or e_slow is None or e_fast <= e_slow: return False, None

        # --- Calculate ATR (使用傳入數據) ---
        if len(closes) < ATR_PERIOD + 1 or len(highs) < ATR_PERIOD + 1 or len(lows) < ATR_PERIOD + 1:
            return False, None
        atr = calculate_atr(highs, lows, closes, ATR_PERIOD)
        if atr is None or atr <= 0: return False, None

        return True, atr
    except Exception as e:
        print(f"ERROR calculating VBO Long signal: {type(e).__name__} {e}") # Log error type
        return False, None

def calculate_vbo_short_signal(closes: List[float], highs: List[float], lows: List[float], vols: List[float]) -> Tuple[bool, Optional[float]]:
    """ (修改) 根據傳入的 K 線數據計算空頭訊號和 ATR """
    try:
        # --- 使用傳入的數據長度進行檢查 ---
        required_len = max(HH_N, VOL_BASE_WIN, ATR_PERIOD) + VOL_LOOKBACK_CONFIRM + 2
        if not all(len(lst) >= required_len for lst in [closes, highs, lows, vols]):
            # print(f"DEBUG: Data length insufficient for short signal. Need {required_len}, Got closes={len(closes)}") # Debug
            return False, None

        # --- VBO Breakdown Logic (使用傳入數據) ---
        curr_close = closes[-1]
        # 確保索引有效
        prev_low_slice_start = -(HH_N + 1)
        if HH_N <= 0 or abs(prev_low_slice_start) >= len(lows):
             if len(lows) >= 2:
                 prev_low = lows[-2]
             else: return False, None
        else:
            prev_low_slice = lows[prev_low_slice_start:-1]
            if not prev_low_slice: return False, None
            prev_low = min(prev_low_slice)

        if curr_close >= prev_low: return False, None
        breakdown_ratio = (prev_low - curr_close) / prev_low if prev_low > 0 else 0
        if breakdown_ratio > OVEREXTEND_CAP: return False, None

        # --- Volume Logic (同上) ---
        confirm_bars = VOL_LOOKBACK_CONFIRM if VOL_LOOKBACK_CONFIRM > 0 else 1
        base_window_len = VOL_BASE_WIN
        total_vol_len_needed = base_window_len + confirm_bars
        if len(vols) < total_vol_len_needed: return False, None

        base_window_end_idx = -confirm_bars
        base_window_start_idx = base_window_end_idx - base_window_len
        if base_window_start_idx < -len(vols) or base_window_end_idx >= 0: return False, None
        base_window = vols[base_window_start_idx:base_window_end_idx]
        if not base_window: return False, None
        try: base_med = statistics.median(base_window)
        except statistics.StatisticsError: return False, None

        recent_slice_start = -confirm_bars
        if abs(recent_slice_start) > len(vols): return False, None
        recent_vols = vols[recent_slice_start:]
        if not recent_vols: return False, None
        recent_sum = sum(recent_vols)
        need_sum = VOL_SPIKE_K * base_med * confirm_bars
        if recent_sum < need_sum: return False, None

        # --- EMA Filter (使用傳入數據) ---
        required_ema_len = EMA_SLOW + 10
        if len(closes) < required_ema_len: return False, None
        segment = closes[-required_ema_len:]
        e_fast = ema(segment, EMA_FAST)
        e_slow = ema(segment, EMA_SLOW)
        if e_fast is None or e_slow is None or e_fast >= e_slow: return False, None # Must be bearish

        # --- Calculate ATR (使用傳入數據) ---
        if len(closes) < ATR_PERIOD + 1 or len(highs) < ATR_PERIOD + 1 or len(lows) < ATR_PERIOD + 1:
            return False, None
        atr = calculate_atr(highs, lows, closes, ATR_PERIOD)
        if atr is None or atr <= 0: return False, None

        return True, atr
    except Exception as e:
        print(f"ERROR calculating VBO Short signal: {type(e).__name__} {e}") # Log error type
        return False, None

# === Thin wrapper for main.py compatibility ===
# 不改動你的計算函數；只補一個 volume_breakout_ok(symbol) 讓 main.py 能呼叫。
from utils import fetch_klines  # 需要這個來抓 REST K 線

def volume_breakout_ok(symbol: str, interval: str = None) -> bool:
    interval = interval or KLINE_INTERVAL
    closes, highs, vols = fetch_klines(symbol, interval, limit=KLINE_LIMIT)
    """
    與舊版相容：抓取 K 線 -> 轉成陣列 -> 丟給 calculate_vbo_long_signal。
    只回傳是否觸發多頭 VBO（你的 main.py 目前只做 LONG）。
    """
    try:
        # 取 K 線（Futures）：回傳通常是 list of [openTime, open, high, low, close, volume, ...]
        kl = fetch_klines(symbol, KLINE_INTERVAL, limit=KLINE_LIMIT)
        if not kl or len(kl) < 50:  # 粗略檢查，避免資料太短
            return False

        # 轉陣列（字串轉 float）
        opens  = [float(x[1]) for x in kl]
        highs  = [float(x[2]) for x in kl]
        lows   = [float(x[3]) for x in kl]
        closes = [float(x[4]) for x in kl]
        vols   = [float(x[5]) for x in kl]

        ok, _atr = calculate_vbo_long_signal(closes, highs, lows, vols)
        return bool(ok)
    except Exception as e:
        # 不 raise，避免把整個快取流程打斷；交給 main 的 try/except 記 WARN
        # 這裡保持安靜或 print 皆可；建議保持安靜，由 main 統一紀錄。
        return False
