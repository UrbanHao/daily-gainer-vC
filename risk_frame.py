from dataclasses import dataclass
from datetime import datetime
from config import DAILY_TARGET_PCT, DAILY_LOSS_CAP, PER_TRADE_RISK, SL_ATR_MULTIPLIER, TP_ATR_MULTIPLIER

@dataclass
class DayState:
    key: str
    pnl_pct: float = 0.0
    trades: int = 0
    halted: bool = False

class DayGuard:
    def __init__(self):
        self.state = DayState(key=datetime.now().date().isoformat())

    def rollover(self):
        k = datetime.now().date().isoformat()
        if k != self.state.key:
            self.state = DayState(key=k)

    def can_trade(self):
        return not self.state.halted

    def on_trade_close(self, pct):
        if self.state.halted: return
        self.state.trades += 1
        self.state.pnl_pct += pct
        if self.state.pnl_pct >= DAILY_TARGET_PCT or self.state.pnl_pct <= DAILY_LOSS_CAP:
            self.state.halted = True

def position_size_notional(equity: float, entry_price: float, atr_value: float) -> float:
    """ (修改) 使用 ATR 計算基於風險的倉位名義價值 """
    if atr_value is None or atr_value <= 0 or entry_price <= 0:
        print(f"Warning: Cannot calculate position size. Invalid inputs: equity={equity}, entry={entry_price}, atr={atr_value}") # Debug
        return 0.0 # 無法計算

    risk_amt_per_trade = equity * PER_TRADE_RISK

    # 計算基於 ATR 的止損距離 (金額)
    stop_loss_distance_money = atr_value * SL_ATR_MULTIPLIER
    # 轉換為止損百分比 (相對於進場價)
    stop_loss_risk_pct = stop_loss_distance_money / entry_price

    if stop_loss_risk_pct <= 0: # 避免除以零或負數
        print(f"Warning: Cannot calculate position size. Stop loss risk percent is <= 0: {stop_loss_risk_pct}") # Debug
        return 0.0

    # 計算名義價值: (每筆風險金額) / (止損百分比)
    notional = risk_amt_per_trade / stop_loss_risk_pct
    # print(f"Debug Pos Size: Eq={equity:.2f}, RiskAmt={risk_amt_per_trade:.2f}, Entry={entry_price}, ATR={atr_value:.4f}, SLDist={stop_loss_distance_money:.4f}, SLPct={stop_loss_risk_pct:.4%}, Notional={notional:.2f}") # Debug
    return max(notional, 0.0)

def compute_bracket(entry: float, side: str, atr_value: float):
    """ (修改) 使用 ATR 計算止損/止盈價格 """
    if atr_value is None or atr_value <= 0:
         print(f"Warning: Cannot compute bracket. Invalid ATR: {atr_value}") # Debug
         return None, None # 無法計算 bracket

    sl_offset = atr_value * SL_ATR_MULTIPLIER
    tp_offset = atr_value * TP_ATR_MULTIPLIER
    if side == "LONG":
        sl = entry - sl_offset
        tp = entry + tp_offset
        # print(f"Debug Bracket LONG: Entry={entry}, ATR={atr_value:.4f}, SL={sl:.4f}, TP={tp:.4f}") # Debug
        return sl, tp
    elif side == "SHORT":
        sl = entry + sl_offset
        tp = entry - tp_offset
        # print(f"Debug Bracket SHORT: Entry={entry}, ATR={atr_value:.4f}, SL={sl:.4f}, TP={tp:.4f}") # Debug
        return sl, tp
    else:
        print(f"Warning: Cannot compute bracket. Invalid side: {side}") # Debug
        return None, None # 無效的 side
