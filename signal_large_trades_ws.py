# file: signal_large_trades_ws.py
from collections import deque, defaultdict
from typing import Optional, Dict, Deque
import math, time, statistics

from config import (
    LARGE_TRADES_ENABLED, LARGE_TRADES_MERGE_S, LARGE_TRADES_FILTER_MODE,
    LARGE_TRADES_BUY_PCT, LARGE_TRADES_SELL_PCT, LARGE_TRADES_BUY_ABS,
    LARGE_TRADES_SELL_ABS, LARGE_TRADES_ANCHOR_DRIFT
)
from ws_client import ws_recent_agg

# 每個 symbol 的歷史視窗總量（做 percentile）
_hist_buy: Dict[str, Deque[float]]  = defaultdict(lambda: deque(maxlen=500))
_hist_sell: Dict[str, Deque[float]] = defaultdict(lambda: deque(maxlen=500))
# 追蹤上次寫入歷史的時間，避免 0.8s 迴圈重複寫入
_last_hist_write: Dict[str, float] = defaultdict(lambda: 0.0)

def _percentile(vals, p):
    if not vals: return math.inf
    s = sorted(vals)
    k = max(0, min(len(s)-1, int(round((p/100.0)*(len(s)-1)))))
    return s[k]
# --- 新增：計算數值在列表中的百分位排名 ---
def _calculate_percentile_rank(data: list, value: float) -> Optional[float]:
    """Calculates the percentile rank of a value within a list."""
    if not data:
        return None
    data.sort() # Ensure data is sorted
    count_below = 0
    count_equal = 0
    for item in data:
        if item < value:
            count_below += 1
        elif item == value:
            count_equal += 1
        else:
            break # Since data is sorted
    if count_equal == 0: # Value not in data, interpolate? For simplicity, use count_below.
         rank = (count_below / len(data)) * 100.0
    else:
        # Standard definition: (count below + 0.5 * count equal) / total count
         rank = ((count_below + 0.5 * count_equal) / len(data)) * 100.0
    return rank
# --- 結束 ---
def large_trades_signal_ws(symbol: str) -> Optional[dict]:
    if not LARGE_TRADES_ENABLED:
        return None

    # 1. 從 WS 快取讀取近 N 秒的成交
    # 我們讀取 MERGE_S + 2 秒的數據，確保滑動窗口是滿的
    rows = ws_recent_agg(symbol, window_s=max(5, LARGE_TRADES_MERGE_S + 2))
    if not rows:
        return {"buy_signal": False, "sell_signal": False} # 回傳預設值

    # 2. 滑窗聚合 (只聚合 MERGE_S 秒內的)
    cutoff_ts = int(time.time() * 1000) - LARGE_TRADES_MERGE_S * 1000
    buy_qty = sell_qty = 0.0
    buy_px_sum = buy_q_sum = 0.0
    sell_px_sum = sell_q_sum = 0.0

    for ts, px, qty, is_buy in rows:
        if ts < cutoff_ts:
            continue # 只聚合 MERGE_S 秒內的

        if is_buy:
            buy_qty += qty
            buy_px_sum += px * qty
            buy_q_sum  += qty
        else:
            sell_qty += qty
            sell_px_sum += px * qty
            sell_q_sum  += qty

    buy_anchor  = (buy_px_sum / buy_q_sum)   if buy_q_sum  > 0 else None
    sell_anchor = (sell_px_sum / sell_q_sum) if sell_q_sum > 0 else None

    # 3. 更新歷史 (用於 percentile)，但限制每秒最多寫一次
    now = time.time()
    if now - _last_hist_write[symbol] > 1.0:
        if buy_qty  > 0: _hist_buy[symbol].append(buy_qty)
        if sell_qty > 0: _hist_sell[symbol].append(sell_qty)
        _last_hist_write[symbol] = now

    # 4. 判斷門檻
    buy_gate = math.inf # 預設不過門檻
    sell_gate = math.inf # 預設不過門檻
    if LARGE_TRADES_FILTER_MODE == "Percentile":
        if _hist_buy[symbol]: # 確保列表不為空
            buy_gate  = _percentile(list(_hist_buy[symbol]),  LARGE_TRADES_BUY_PCT)
        if _hist_sell[symbol]: # 確保列表不為空
            sell_gate = _percentile(list(_hist_sell[symbol]), LARGE_TRADES_SELL_PCT)
    else: # Absolute Mode
        buy_gate, sell_gate = LARGE_TRADES_BUY_ABS, LARGE_TRADES_SELL_ABS

    # --- 新增：計算當前成交量的百分位排名 ---
    buy_pct_rank = None
    sell_pct_rank = None
    if LARGE_TRADES_FILTER_MODE == "Percentile":
        hist_buy_list = list(_hist_buy[symbol])
        hist_sell_list = list(_hist_sell[symbol])
        if buy_qty > 0 and hist_buy_list:
             buy_pct_rank = _calculate_percentile_rank(hist_buy_list, buy_qty)
        if sell_qty > 0 and hist_sell_list:
             sell_pct_rank = _calculate_percentile_rank(hist_sell_list, sell_qty)
    # --- 結束 ---

    return {
        "buy_signal":  buy_qty  > buy_gate  and buy_anchor  is not None, # 進場判斷 (維持用 gate)
        "sell_signal": sell_qty > sell_gate and sell_anchor is not None, # 進場判斷 (維持用 gate)
        "buy_vol": buy_qty, "sell_vol": sell_qty,
        "buy_anchor": buy_anchor, "sell_anchor": sell_anchor,
        "buy_gate": buy_gate, "sell_gate": sell_gate,
        "buy_pct_rank": buy_pct_rank,   # <-- 新增回傳值
        "sell_pct_rank": sell_pct_rank, # <-- 新增回傳值
    }

def near_anchor_ok(price: float, anchor: Optional[float]) -> bool:
    """檢查現價是否在 anchor 的 ±drift% 範圍內"""
    if not anchor or not price or anchor == 0: return False
    drift = LARGE_TRADES_ANCHOR_DRIFT
    return (anchor * (1 - drift) <= price <= anchor * (1 + drift))
