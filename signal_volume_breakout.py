import statistics
from config import (
    KLINE_INTERVAL, KLINE_LIMIT, HH_N, OVEREXTEND_CAP,
    VOL_BASE_WIN, VOL_SPIKE_K, VOL_LOOKBACK_CONFIRM,
    EMA_FAST, EMA_SLOW, ATR_PERIOD
)
# --- 移除 fetch_klines 的 import ---
from utils import fetch_klines, ema, calculate_atr
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

def volume_breakout_ok(symbol: str, interval: str = None) -> bool:
    """
    與舊版介面相同：只回傳 bool。
    內部統一用 utils.fetch_klines，且接收 4 個 list，避免「expected 4, got 2」。
    """
    try:
        interval = interval or KLINE_INTERVAL

        # 1) 取 K 線（四個 list）
        closes, highs, lows, vols = fetch_klines(symbol, interval, limit=KLINE_LIMIT)
        if not closes or not highs or not lows or not vols:
            return False

        # 2) 高點突破（避免過度延伸）
        curr_close = closes[-1]
        if HH_N > 0 and len(highs) > HH_N + 1:
            prev_high = max(highs[-(HH_N + 1):-1])
        elif len(highs) >= 2:
            prev_high = highs[-2]
        else:
            return False

        if prev_high <= 0 or curr_close <= prev_high:
            return False

        breakout_ratio = (curr_close - prev_high) / prev_high
        if breakout_ratio > OVEREXTEND_CAP:
            return False

        # 3) 量能條件（最近 N 根總量 > 中位數 * K 倍 * N）
        confirm_bars = max(1, VOL_LOOKBACK_CONFIRM)
        if len(vols) < VOL_BASE_WIN + confirm_bars:
            return False

        base_window = vols[-(VOL_BASE_WIN + confirm_bars):-confirm_bars]
        if not base_window:
            return False

        base_med = statistics.median(base_window)
        recent_sum = sum(vols[-confirm_bars:])
        if recent_sum < VOL_SPIKE_K * base_med * confirm_bars:
            return False

        # 4) EMA 多頭過濾
        need = EMA_SLOW + 10
        if len(closes) < need:
            return False
        seg = closes[-need:]
        e_fast = ema(seg, EMA_FAST)
        e_slow = ema(seg, EMA_SLOW)
        if e_fast is None or e_slow is None or e_fast <= e_slow:
            return False

        # 5) ATR 有效（僅確認非零）
        atr = calculate_atr(highs, lows, closes, ATR_PERIOD)
        if atr is None or atr <= 0:
            return False

        return True

    except Exception:
        # 保持安靜，讓外層統一記錄 WARN
        return False
