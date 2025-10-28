import statistics
from config import (KLINE_INTERVAL, KLINE_LIMIT, HH_N, OVEREXTEND_CAP,
                    VOL_BASE_WIN, VOL_SPIKE_K, VOL_LOOKBACK_CONFIRM,
                    EMA_FAST, EMA_SLOW, ATR_PERIOD)
# --- 移除 fetch_klines 的 import ---
from utils import ema, calculate_atr # 只 import 需要的計算函數
from typing import Tuple, Optional, List # <-- 加入 List

# --- 修改：訊號函數接收 K 線數據 ---
def calculate_vbo_long_signal(closes: List[float], highs: List[float], lows: List[float], vols: List[float]) -> Tuple[bool, Optional[float]]:
    """ (修改) 根據傳入的 K 線數據計算多頭訊號和 ATR """
    try:
        # --- 使用傳入的數據長度進行檢查 ---
        required_len = max(HH_N, VOL_BASE_WIN, ATR_PERIOD) + VOL_LOOKBACK_CONFIRM + 2
        if len(closes) < required_len or len(highs) < required_len or len(lows) < required_len or len(vols) < required_len:
            # print(f"DEBUG: Data length insufficient for long signal. Need {required_len}, Got closes={len(closes)}") # Debug
            return False, None # 數據不足

        # --- VBO Breakout Logic (使用傳入數據) ---
        curr_close = closes[-1]
        # 確保索引有效
        if HH_N > 0 and len(highs) > HH_N + 1:
             prev_high_slice = highs[-(HH_N + 1):-1]
             if not prev_high_slice: return False, None # Slice is empty
             prev_high = max(prev_high_slice)
        elif len(highs) >= 2:
             prev_high = highs[-2]
        else:
             return False, None # Not enough data for prev_high

        if curr_close <= prev_high: return False, None
        breakout_ratio = (curr_close - prev_high) / prev_high if prev_high > 0 else 0
        if breakout_ratio > OVEREXTEND_CAP: return False, None

        # --- Volume Logic (使用傳入數據) ---
        base_window_end_idx = -VOL_LOOKBACK_CONFIRM if VOL_LOOKBACK_CONFIRM > 0 else len(vols)
        base_window_start_idx = base_window_end_idx - VOL_BASE_WIN

        # 再次檢查索引有效性
        if base_window_start_idx < 0 or base_window_end_idx > len(vols) or base_window_start_idx >= base_window_end_idx:
             # print(f"DEBUG: Invalid volume window indices. Start={base_window_start_idx}, End={base_window_end_idx}, Len={len(vols)}") # Debug
             return False, None

        base_window = vols[base_window_start_idx:base_window_end_idx]
        if not base_window: return False, None # Avoid error on empty median

        try:
             base_med = statistics.median(base_window)
        except statistics.StatisticsError: # Handle cases with too few data points for median
             # print(f"DEBUG: StatisticsError calculating median for base_window (len={len(base_window)})") # Debug
             return False, None


        recent_slice_start = -VOL_LOOKBACK_CONFIRM if VOL_LOOKBACK_CONFIRM > 0 else -1
        if abs(recent_slice_start) > len(vols): return False, None # Index out of bounds
        recent_vols = vols[recent_slice_start:]
        if not recent_vols: return False, None # Should not happen if len check passed

        recent_sum = sum(recent_vols)
        confirm_count = len(recent_vols) # Use actual count in slice
        need_sum = VOL_SPIKE_K * base_med * confirm_count
        if recent_sum < need_sum: return False, None

        # --- EMA Filter (使用傳入數據) ---
        required_ema_len = EMA_SLOW + 10
        if len(closes) < required_ema_len: return False, None
        segment = closes[-required_ema_len:]
        e_fast = ema(segment, EMA_FAST)
        e_slow = ema(segment, EMA_SLOW)
        if e_fast is None or e_slow is None or e_fast <= e_slow: return False, None

        # --- Calculate ATR (使用傳入數據) ---
        if len(closes) < ATR_PERIOD + 1: return False, None
        # 確認 highs, lows 也足夠長
        if len(highs) < ATR_PERIOD + 1 or len(lows) < ATR_PERIOD + 1: return False, None
        atr = calculate_atr(highs, lows, closes, ATR_PERIOD)
        if atr is None or atr <= 0: return False, None

        return True, atr
    except Exception as e:
        print(f"ERROR calculating VBO Long signal: {e}") # Log error
        return False, None

def calculate_vbo_short_signal(closes: List[float], highs: List[float], lows: List[float], vols: List[float]) -> Tuple[bool, Optional[float]]:
    """ (修改) 根據傳入的 K 線數據計算空頭訊號和 ATR """
    try:
        # --- 使用傳入的數據長度進行檢查 ---
        required_len = max(HH_N, VOL_BASE_WIN, ATR_PERIOD) + VOL_LOOKBACK_CONFIRM + 2
        if len(closes) < required_len or len(highs) < required_len or len(lows) < required_len or len(vols) < required_len:
            # print(f"DEBUG: Data length insufficient for short signal. Need {required_len}, Got closes={len(closes)}") # Debug
            return False, None

        # --- VBO Breakdown Logic (使用傳入數據) ---
        curr_close = closes[-1]
        # 確保索引有效
        if HH_N > 0 and len(lows) > HH_N + 1:
             prev_low_slice = lows[-(HH_N + 1):-1]
             if not prev_low_slice: return False, None
             prev_low = min(prev_low_slice)
        elif len(lows) >= 2:
             prev_low = lows[-2]
        else:
             return False, None

        if curr_close >= prev_low: return False, None
        breakdown_ratio = (prev_low - curr_close) / prev_low if prev_low > 0 else 0
        if breakdown_ratio > OVEREXTEND_CAP: return False, None

        # --- Volume Logic (同上) ---
        base_window_end_idx = -VOL_LOOKBACK_CONFIRM if VOL_LOOKBACK_CONFIRM > 0 else len(vols)
        base_window_start_idx = base_window_end_idx - VOL_BASE_WIN
        if base_window_start_idx < 0 or base_window_end_idx > len(vols) or base_window_start_idx >= base_window_end_idx:
             return False, None
        base_window = vols[base_window_start_idx:base_window_end_idx]
        if not base_window: return False, None
        try:
             base_med = statistics.median(base_window)
        except statistics.StatisticsError:
             return False, None

        recent_slice_start = -VOL_LOOKBACK_CONFIRM if VOL_LOOKBACK_CONFIRM > 0 else -1
        if abs(recent_slice_start) > len(vols): return False, None
        recent_vols = vols[recent_slice_start:]
        if not recent_vols: return False, None
        recent_sum = sum(recent_vols)
        confirm_count = len(recent_vols)
        need_sum = VOL_SPIKE_K * base_med * confirm_count
        if recent_sum < need_sum: return False, None

        # --- EMA Filter (使用傳入數據) ---
        required_ema_len = EMA_SLOW + 10
        if len(closes) < required_ema_len: return False, None
        segment = closes[-required_ema_len:]
        e_fast = ema(segment, EMA_FAST)
        e_slow = ema(segment, EMA_SLOW)
        if e_fast is None or e_slow is None or e_fast >= e_slow: return False, None # Must be bearish

        # --- Calculate ATR (使用傳入數據) ---
        if len(closes) < ATR_PERIOD + 1: return False, None
        if len(highs) < ATR_PERIOD + 1 or len(lows) < ATR_PERIOD + 1: return False, None
        atr = calculate_atr(highs, lows, closes, ATR_PERIOD)
        if atr is None or atr <= 0: return False, None

        return True, atr
    except Exception as e:
        print(f"ERROR calculating VBO Short signal: {e}") # Log error
        return False, None

# --- 保留舊函數但標記為已棄用 (可選，或直接刪除) ---
# def get_vbo_long_signal(*args, **kwargs):
#     # raise DeprecationWarning("Use calculate_vbo_long_signal with kline data instead.")
#     print("WARNING: Deprecated get_vbo_long_signal called. API inefficiency.")
#     symbol = args[0] if args else kwargs.get('symbol')
#     if not symbol: return False, None
#     try:
#         klines_data = fetch_klines(symbol, KLINE_INTERVAL, KLINE_LIMIT)
#         return calculate_vbo_long_signal(*klines_data)
#     except: return False, None
#
# def get_vbo_short_signal(*args, **kwargs):
#     # raise DeprecationWarning("Use calculate_vbo_short_signal with kline data instead.")
#     print("WARNING: Deprecated get_vbo_short_signal called. API inefficiency.")
#     symbol = args[0] if args else kwargs.get('symbol')
#     if not symbol: return False, None
#     try:
#         klines_data = fetch_klines(symbol, KLINE_INTERVAL, KLINE_LIMIT)
#         return calculate_vbo_short_signal(*klines_data)
#     except: return False, None
