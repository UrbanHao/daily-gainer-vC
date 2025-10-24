import os, hmac, hashlib, requests
from utils import now_ts_ms, SESSION, BINANCE_FUTURES_BASE

class SimAdapter:
    def __init__(self):
        self.open = None
    def has_open(self): return self.open is not None
    def best_price(self, symbol):
        r = SESSION.get(f"{BINANCE_FUTURES_BASE}/fapi/v1/ticker/price", params={"symbol":symbol}, timeout=5)
        r.raise_for_status()
        return float(r.json()["price"])
    def place_bracket(self, symbol, side, qty, entry, sl, tp):
        self.open = {"symbol":symbol, "side":side, "qty":qty, "entry":entry, "sl":sl, "tp":tp}
        return "SIM-ORDER"
    def poll_and_close_if_hit(self, day_guard):
        if not self.open: return False, None, None
        p = self.best_price(self.open["symbol"])
        side = self.open["side"]
        hit_tp = (p >= self.open["tp"]) if side=="LONG" else (p <= self.open["tp"])
        hit_sl = (p <= self.open["sl"]) if side=="LONG" else (p >= self.open["sl"])
        if hit_tp or hit_sl:
            exit_price = self.open["tp"] if hit_tp else self.open["sl"]
            pct = (exit_price - self.open["entry"]) / self.open["entry"]
            if side == "SHORT": pct = -pct
            symbol = self.open["symbol"]
            self.open = None
            day_guard.on_trade_close(pct)
            return True, pct, symbol
        return False, None, None

class LiveAdapter:
    """
    實盤：請把 place_bracket / poll_and_close_if_hit 換成你的 OCO/條件單流程。
    這裡只保留接口。
    """
    def __init__(self):
        self.key = os.getenv("BINANCE_API_KEY", "")
        self.secret = os.getenv("BINANCE_SECRET", "")
        self.open = None
    def _sign(self, params:dict):
        q = "&".join([f"{k}={params[k]}" for k in sorted(params.keys())])
        sig = hmac.new(self.secret.encode(), q.encode(), hashlib.sha256).hexdigest()
        return q + "&signature=" + sig
    def _post(self, path, params):
        params["timestamp"] = now_ts_ms()
        qs = self._sign(params)
        r = SESSION.post(f"{BINANCE_FUTURES_BASE}{path}?{qs}", headers={"X-MBX-APIKEY": self.key}, timeout=10)
        r.raise_for_status()
        return r.json()
    def has_open(self): return self.open is not None
    def best_price(self, symbol):
        r = SESSION.get(f"{BINANCE_FUTURES_BASE}/fapi/v1/ticker/price", params={"symbol":symbol}, timeout=5)
        r.raise_for_status()
        return float(r.json()["price"])
    def place_bracket(self, symbol, side, qty, entry, sl, tp):
        # TODO: 以你的實盤邏輯下單，委託成功後設定 self.open
        self.open = {"symbol":symbol, "side":side, "qty":qty, "entry":entry, "sl":sl, "tp":tp}
        return "LIVE-ORDER"
    def poll_and_close_if_hit(self, day_guard):
        if not self.open: return False, None, None
        p = self.best_price(self.open["symbol"])
        side = self.open["side"]
        hit_tp = (p >= self.open["tp"]) if side=="LONG" else (p <= self.open["tp"])
        hit_sl = (p <= self.open["sl"]) if side=="LONG" else (p >= self.open["sl"])
        if hit_tp or hit_sl:
            exit_price = self.open["tp"] if hit_tp else self.open["sl"]
            pct = (exit_price - self.open["entry"]) / self.open["entry"]
            if side == "SHORT": pct = -pct
            symbol = self.open["symbol"]
            # TODO: 撤另一條條件單 & 平倉單
            self.open = None
            day_guard.on_trade_close(pct)
            return True, pct, symbol
        return False, None, None