from dataclasses import dataclass
from datetime import datetime
from config import DAILY_TARGET_PCT, DAILY_LOSS_CAP, PER_TRADE_RISK, SL_ATR_MULTIPLIER, TP_ATR_MULTIPLIER
from typing import Tuple, Optional
# --- Use Decimal for configuration values ---
from decimal import Decimal, InvalidOperation

# Convert config floats to Decimal once
try:
    PER_TRADE_RISK_DEC = Decimal(str(PER_TRADE_RISK))
    SL_ATR_MULT_DEC = Decimal(str(SL_ATR_MULTIPLIER))
    TP_ATR_MULT_DEC = Decimal(str(TP_ATR_MULTIPLIER))
except InvalidOperation:
    print("CRITICAL ERROR: Could not convert risk config values to Decimal. Check config.py.")
    # Set fallback defaults or raise an error
    PER_TRADE_RISK_DEC = Decimal('0.0075')
    SL_ATR_MULT_DEC = Decimal('1.5')
    TP_ATR_MULT_DEC = Decimal('3.0')


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
        try:
             # Ensure pct is a number before calculation
             pct_float = float(pct)
             self.state.trades += 1
             # Use float for state tracking as Decimal precision might not be needed here
             self.state.pnl_pct += pct_float
             # Check halt conditions (using floats is likely fine here)
             if self.state.pnl_pct >= DAILY_TARGET_PCT or self.state.pnl_pct <= DAILY_LOSS_CAP:
                 self.state.halted = True
        except (ValueError, TypeError):
             print(f"Warning: Invalid PnL percentage received in on_trade_close: {pct}")


def position_size_notional(equity: float, entry_price: float, atr_value: float) -> float:
    """ 使用 ATR 計算基於風險的倉位名義價值 (內部使用 Decimal) """
    try:
        # Convert inputs to Decimal safely using the helper
        from utils import to_decimal # Import helper here or move it to a common place
        eq_dec = to_decimal(equity)
        entry_dec = to_decimal(entry_price)
        atr_dec = to_decimal(atr_value)

        # Check if conversions were successful and values are valid
        if not eq_dec.is_finite() or not entry_dec.is_finite() or entry_dec <= Decimal(0) or \
           not atr_dec.is_finite() or atr_dec <= Decimal(0):
            print(f"Warning: Cannot calculate position size due to invalid inputs: equity={equity}, entry={entry_price}, atr={atr_value}")
            return 0.0

        risk_amt_per_trade = eq_dec * PER_TRADE_RISK_DEC

        stop_loss_distance_money = atr_dec * SL_ATR_MULT_DEC
        # Ensure distance is positive
        if stop_loss_distance_money <= Decimal(0):
             print(f"Warning: Calculated stop loss distance is not positive: {stop_loss_distance_money}")
             return 0.0

        stop_loss_risk_pct = stop_loss_distance_money / entry_dec

        if stop_loss_risk_pct <= Decimal(0):
            print(f"Warning: Cannot calculate position size. Stop loss risk percent is <= 0: {stop_loss_risk_pct}")
            return 0.0

        notional_dec = risk_amt_per_trade / stop_loss_risk_pct
        result = float(max(notional_dec, Decimal(0)))
        # print(f"Debug Pos Size: Eq={equity:.2f}, RiskAmt={risk_amt_per_trade:.2f}, Entry={entry_price}, ATR={atr_value:.4f}, SLDist={stop_loss_distance_money:.4f}, SLPct={stop_loss_risk_pct:.4%}, Notional={result:.2f}") # Debug
        return result

    except (InvalidOperation, TypeError, ZeroDivisionError) as e:
        print(f"Error in position_size_notional calculation: {e}")
        return 0.0


def compute_bracket(entry: float, side: str, atr_value: float) -> Tuple[Optional[float], Optional[float]]:
    """ 使用 ATR 計算止損/止盈價格 (內部使用 Decimal, 回傳 float tuple 或 None tuple) """
    try:
        from utils import to_decimal # Import helper
        entry_dec = to_decimal(entry)
        atr_dec = to_decimal(atr_value)

        # Validate inputs
        if not entry_dec.is_finite() or not atr_dec.is_finite() or atr_dec <= Decimal(0):
            print(f"Warning: Cannot compute bracket due to invalid inputs: entry={entry}, atr={atr_value}")
            return None, None

        sl_offset = atr_dec * SL_ATR_MULT_DEC
        tp_offset = atr_dec * TP_ATR_MULT_DEC

        if side == "LONG":
            sl_dec = entry_dec - sl_offset
            tp_dec = entry_dec + tp_offset
        elif side == "SHORT":
            sl_dec = entry_dec + sl_offset
            tp_dec = entry_dec - tp_offset
        else:
            print(f"Warning: Cannot compute bracket. Invalid side: {side}")
            return None, None

        # Ensure SL/TP are still finite after calculation
        if not sl_dec.is_finite() or not tp_dec.is_finite():
             print(f"Warning: SL or TP calculation resulted in non-finite value. SL={sl_dec}, TP={tp_dec}")
             return None, None

        # Ensure SL and TP maintain correct relationship with entry
        if side == "LONG" and (sl_dec >= entry_dec or tp_dec <= entry_dec):
             print(f"Warning: Invalid LONG bracket SL={sl_dec} >= Entry={entry_dec} or TP={tp_dec} <= Entry={entry_dec}")
             # Optionally adjust or return None
             return None, None # Safer to return None if logic fails
        if side == "SHORT" and (sl_dec <= entry_dec or tp_dec >= entry_dec):
             print(f"Warning: Invalid SHORT bracket SL={sl_dec} <= Entry={entry_dec} or TP={tp_dec} >= Entry={entry_dec}")
             return None, None

        # print(f"Debug Bracket {side}: Entry={entry:.4f}, ATR={atr_value:.4f}, SL={float(sl_dec):.4f}, TP={float(tp_dec):.4f}") # Debug
        return float(sl_dec), float(tp_dec)

    except (InvalidOperation, TypeError) as e:
        print(f"Error in compute_bracket calculation: {e}")
        return None, None
