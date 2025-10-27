import statistics
from config import (KLINE_INTERVAL, KLINE_LIMIT, HH_N, OVEREXTEND_CAP,
                    VOL_BASE_WIN, VOL_SPIKE_K, VOL_LOOKBACK_CONFIRM,
                    EMA_FAST, EMA_SLOW, ATR_PERIOD) # <-- 匯入 ATR_PERIOD
from utils import fetch_klines, ema, calculate_atr # <-- 匯入 calculate_atr
from typing import Tuple, Optional # <-- 新增

def get_vbo_long_signal(symbol: str) -> Tuple[bool, Optional[float]]:
    """ (修改) 檢查多頭訊號，並回傳 (訊號是否成立, ATR值 或 None) """
    try:
        closes, highs, lows, vols = fetch_klines(symbol, KLINE_INTERVAL, KLINE_LIMIT) # 接收 lows
        required_len = max(HH_N, VOL_BASE_WIN, ATR_PERIOD) + VOL_LOOKBACK_CONFIRM + 2 # 計算所需最小長度
        if len(closes) < required_len:
            # print(f"Not enough data for {symbol}: need {required_len}, got {len(closes)}") # Debug
            return False, None # 確保數據足夠 ATR 和其他計算

        # --- VBO Breakout Logic ---
        curr_close = closes[-1]
        prev_high = max(highs[-(HH_N + 1):-1]) if HH_N > 0 else highs[-2] # Handle HH_N=0 case
        if curr_close <= prev_high:
            return False, None

        breakout_ratio = (curr_close - prev_high) / prev_high if prev_high > 0 else 0
        if breakout_ratio > OVEREXTEND_CAP:
            return False, None

        # --- Volume Logic ---
        base_window_end = -VOL_LOOKBACK_CONFIRM if VOL_LOOKBACK_CONFIRM > 0 else len(vols)
        base_window_start = -(VOL_BASE_WIN + VOL_LOOKBACK_CONFIRM) if VOL_LOOKBACK_CONFIRM > 0 else -VOL_BASE_WIN
        # Ensure indices are valid
        if abs(base_window_start) > len(vols) or base_window_end > 0 :
             return False, None # Window index out of bounds

        base_window = vols[base_window_start:base_window_end]
        if not base_window: return False, None # Avoid error on empty median

        base_med = statistics.median(base_window)
        recent_sum = sum(vols[-VOL_LOOKBACK_CONFIRM:]) if VOL_LOOKBACK_CONFIRM > 0 else vols[-1]
        need_sum = VOL_SPIKE_K * base_med * (VOL_LOOKBACK_CONFIRM if VOL_LOOKBACK_CONFIRM > 0 else 1)
        if recent_sum < need_sum:
            return False, None

        # --- EMA Filter ---
        required_ema_len = EMA_SLOW + 10 # Need enough data for stable EMA
        if len(closes) < required_ema_len: return False, None
        segment = closes[-required_ema_len:] # Ensure segment is long enough

        e_fast = ema(segment, EMA_FAST)
        e_slow = ema(segment, EMA_SLOW)
        if e_fast is None or e_slow is None or e_fast <= e_slow: # Must be bullish alignment
            return False, None

        # --- Calculate ATR ---
        # Ensure we pass enough data for ATR calculation including the lookback + 1
        if len(closes) < ATR_PERIOD + 1: return False, None
        atr = calculate_atr(highs, lows, closes, ATR_PERIOD)
        if atr is None or atr <= 0: # Ensure ATR is valid
            # print(f"Invalid ATR for {symbol}: {atr}") # Debug
            return False, None

        return True, atr
    except Exception as e:
        # print(f"Error in get_vbo_long_signal for {symbol}: {e}") # Debug
        return False, None

def get_vbo_short_signal(symbol: str) -> Tuple[bool, Optional[float]]:
    """ (修改) 檢查空頭訊號，並回傳 (訊號是否成立, ATR值 或 None) """
    try:
        closes, highs, lows, vols = fetch_klines(symbol, KLINE_INTERVAL, KLINE_LIMIT)
        required_len = max(HH_N, VOL_BASE_WIN, ATR_PERIOD) + VOL_LOOKBACK_CONFIRM + 2
        if len(closes) < required_len:
            # print(f"Not enough data for {symbol}: need {required_len}, got {len(closes)}") # Debug
            return False, None

        # --- VBO Breakdown Logic ---
        curr_close = closes[-1]
        prev_low = min(lows[-(HH_N + 1):-1]) if HH_N > 0 else lows[-2] # Handle HH_N=0 case
        if curr_close >= prev_low:
            return False, None

        breakdown_ratio = (prev_low - curr_close) / prev_low if prev_low > 0 else 0
        if breakdown_ratio > OVEREXTEND_CAP:
            return False, None

        # --- Volume Logic (same as long) ---
        base_window_end = -VOL_LOOKBACK_CONFIRM if VOL_LOOKBACK_CONFIRM > 0 else len(vols)
        base_window_start = -(VOL_BASE_WIN + VOL_LOOKBACK_CONFIRM) if VOL_LOOKBACK_CONFIRM > 0 else -VOL_BASE_WIN
        if abs(base_window_start) > len(vols) or base_window_end > 0:
            return False, None
        base_window = vols[base_window_start:base_window_end]
        if not base_window: return False, None

        base_med = statistics.median(base_window)
        recent_sum = sum(vols[-VOL_LOOKBACK_CONFIRM:]) if VOL_LOOKBACK_CONFIRM > 0 else vols[-1]
        need_sum = VOL_SPIKE_K * base_med * (VOL_LOOKBACK_CONFIRM if VOL_LOOKBACK_CONFIRM > 0 else 1)
        if recent_sum < need_sum:
            return False, None

        # --- EMA Filter ---
        required_ema_len = EMA_SLOW + 10
        if len(closes) < required_ema_len: return False, None
        segment = closes[-required_ema_len:]

        e_fast = ema(segment, EMA_FAST)
        e_slow = ema(segment, EMA_SLOW)
        if e_fast is None or e_slow is None or e_fast >= e_slow: # Must be bearish alignment
            return False, None

        # --- Calculate ATR ---
        if len(closes) < ATR_PERIOD + 1: return False, None
        atr = calculate_atr(highs, lows, closes, ATR_PERIOD)
        if atr is None or atr <= 0:
            # print(f"Invalid ATR for {symbol}: {atr}") # Debug
            return False, None

        return True, atr
    except Exception as e:
        # print(f"Error in get_vbo_short_signal for {symbol}: {e}") # Debug
        return False, None
