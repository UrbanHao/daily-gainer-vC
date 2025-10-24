import os, hmac, hashlib, requests, time
from utils import now_ts_ms, SESSION, BINANCE_FUTURES_BASE
from config import USE_TESTNET, ORDER_TIMEOUT_SEC

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
        r = SESSION.get(f"{BINANCE_FUTURES_BASE}/fapi/v1/ticker/price", params={"symbol":self.open["symbol"]}, timeout=5)
        r.raise_for_status()
        p = float(r.json()["price"])
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
    Binance USDT-M Futures — 限價進場 + 兩條互斥條件單（TP/SL, closePosition=true）
    單向倉邏輯；先用測試網 (USE_TESTNET=True) 驗證全流程。
    """
    def __init__(self):
        self.key = os.getenv("BINANCE_API_KEY", "")
        self.secret = os.getenv("BINANCE_SECRET", "")
        self.base = "https://testnet.binancefuture.com" if USE_TESTNET else BINANCE_FUTURES_BASE
        self.open = None  # {symbol, side, qty, entry, sl, tp, entryId, tpId, slId}

    def _sign(self, params:dict):
        q = "&".join([f"{k}={params[k]}" for k in sorted(params.keys())])
        sig = hmac.new(self.secret.encode(), q.encode(), hashlib.sha256).hexdigest()
        return q + "&signature=" + sig
    def _post(self, path, params):
        params["timestamp"] = now_ts_ms()
        qs = self._sign(params)
        r = SESSION.post(f"{self.base}{path}?{qs}", headers={"X-MBX-APIKEY": self.key}, timeout=10)
        r.raise_for_status()
        return r.json()
    def _get(self, path, params):
        params = dict(params or {})
        params["timestamp"] = now_ts_ms()
        qs = self._sign(params)
        r = SESSION.get(f"{self.base}{path}?{qs}", headers={"X-MBX-APIKEY": self.key}, timeout=10)
        r.raise_for_status()
        return r.json()
    def _delete(self, path, params):
        params["timestamp"] = now_ts_ms()
        qs = self._sign(params)
        r = SESSION.delete(f"{self.base}{path}?{qs}", headers={"X-MBX-APIKEY": self.key}, timeout=10)
        r.raise_for_status()
        return r.json()

    def has_open(self): return self.open is not None
    def best_price(self, symbol):
        r = SESSION.get(f"{self.base}/fapi/v1/ticker/price", params={"symbol":symbol}, timeout=5)
        r.raise_for_status()
        return float(r.json()["price"])

    def place_bracket(self, symbol, side, qty, entry, sl, tp):
        """
        1) 限價進場（GTC）
        2) 等待成交（逾時自動撤單）
        3) 成交後掛 reduceOnly 出場單：TAKE_PROFIT_MARKET 與 STOP_MARKET（closePosition=true）
        """
        if side not in ("LONG","SHORT"):
            raise ValueError("side must be LONG/SHORT")
        order_side = "BUY" if side=="LONG" else "SELL"

        # 1) 限價進場
        entry_params = {
            "symbol": symbol,
            "side": order_side,
            "type": "LIMIT",
            "timeInForce": "GTC",
            "quantity": f"{qty}",
            "price": f"{entry}",
            "newClientOrderId": f"entry_{int(time.time())}"
        }
        entry_res = self._post("/fapi/v1/order", entry_params)
        entry_id = entry_res["orderId"]

        # 2) 等待成交或逾時撤單
        t0 = time.time()
        filled = False
        while time.time() - t0 < ORDER_TIMEOUT_SEC:
            q = self._get("/fapi/v1/order", {"symbol":symbol, "orderId":entry_id})
            if q.get("status") == "FILLED":
                filled = True
                break
            time.sleep(0.6)
        if not filled:
            try:
                self._delete("/fapi/v1/order", {"symbol":symbol, "orderId":entry_id})
            finally:
                self.open = None
            raise TimeoutError("Entry limit order not filled within timeout; canceled.")

        # 3) 掛 TP/SL（closePosition=true 以整倉平倉；等同 reduce-only 全部倉位）
        exit_side = "SELL" if side=="LONG" else "BUY"
        tp_res = self._post("/fapi/v1/order", {
            "symbol": symbol,
            "side": exit_side,
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": f"{tp}",
            "closePosition": "true",
            "workingType": "CONTRACT_PRICE"
        })
        sl_res = self._post("/fapi/v1/order", {
            "symbol": symbol,
            "side": exit_side,
            "type": "STOP_MARKET",
            "stopPrice": f"{sl}",
            "closePosition": "true",
            "workingType": "CONTRACT_PRICE"
        })

        self.open = {
            "symbol":symbol, "side":side, "qty":qty,
            "entry":entry, "sl":sl, "tp":tp,
            "entryId": entry_id,
            "tpId": tp_res["orderId"], "slId": sl_res["orderId"]
        }
        return str(entry_id)

    def poll_and_close_if_hit(self, day_guard):
        """
        查詢 TP/SL 狀態；若任一成交，計算 PnL，撤另一條，回報 DayGuard
        """
        if not self.open: return False, None, None
        symbol = self.open["symbol"]
        side   = self.open["side"]
        entry  = float(self.open["entry"])
        tpId   = self.open["tpId"]
        slId   = self.open["slId"]

        tp_q = self._get("/fapi/v1/order", {"symbol":symbol, "orderId":tpId})
        sl_q = self._get("/fapi/v1/order", {"symbol":symbol, "orderId":slId})
        tp_filled = tp_q.get("status") == "FILLED"
        sl_filled = sl_q.get("status") == "FILLED"

        if not tp_filled and not sl_filled:
            return False, None, None

        exit_price = float(self.open["tp"] if tp_filled else self.open["sl"])
        pct = (exit_price - entry) / entry
        if side == "SHORT": pct = -pct

        try:
            other_id = slId if tp_filled else tpId
            other_q  = self._get("/fapi/v1/order", {"symbol":symbol, "orderId":other_id})
            if other_q.get("status") in ("NEW","PARTIALLY_FILLED"):
                self._delete("/fapi/v1/order", {"symbol":symbol, "orderId":other_id})
        except Exception:
            pass

        self.open = None
        day_guard.on_trade_close(pct)
        return True, pct, symbol
