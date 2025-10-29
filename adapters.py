import os, hmac, hashlib, requests, time
from typing import Tuple, Optional

# ✅ 公開 REST 統一走 _rest_json（含 202/429/5xx 退避與多 host 輪詢）
from utils import _rest_json, now_ts_ms, SESSION, TIME_OFFSET_MS, ws_best_price, EXCHANGE_INFO

# ✅ 這兩個常數要從 config 匯入（不是 utils）
from config import USE_TESTNET, ORDER_TIMEOUT_SEC, BINANCE_FUTURES_BASE, BINANCE_FUTURES_TEST_BASE

try:
    TIME_OFFSET_MS
except NameError:
    TIME_OFFSET_MS = 0  # fallback if not imported

try:
    from ws_client import ws_best_price as _ws_best_price
except Exception:
    _ws_best_price = None

def _public_get_json(path: str, params=None, timeout=5, tries=3):
    """
    只給 adapters.py 用的公開端點請求器。
    不改任何對外介面，純粹把 SESSION.get 換成 _rest_json。
    """
    return _rest_json(path, params=params or {}, timeout=timeout, tries=tries)


class SimAdapter:
    def __init__(self):
        self.open = None

    def has_open(self):
        return self.open is not None

    def best_price(self, symbol: str) -> float:
        # 先試 WS
        if _ws_best_price:
            try:
                p = _ws_best_price(symbol)
                if p is not None:
                    return float(p)
            except Exception:
                pass
        # 後備：公開 REST
        data = _public_get_json("/fapi/v1/ticker/price",
                                params={"symbol": symbol}, timeout=5, tries=3)
        price = float(data.get("price"))
        return price  # ✅ 修正：不要再回 r.json()

    def place_bracket(self, symbol, side, qty, entry, sl, tp):
        self.open = {"symbol": symbol, "side": side, "qty": qty,
                     "entry": entry, "sl": sl, "tp": tp}
        return "SIM-ORDER"

    def poll_and_close_if_hit(self, day_guard):
        if not self.open:
            return False, None, None
        try:
            p = self.best_price(self.open["symbol"])
        except Exception:
            return False, None, None
        side = self.open["side"]
        hit_tp = (p >= self.open["tp"]) if side == "LONG" else (p <= self.open["tp"])
        hit_sl = (p <= self.open["sl"]) if side == "LONG" else (p >= self.open["sl"])
        if hit_tp or hit_sl:
            exit_price = self.open["tp"] if hit_tp else self.open["sl"]
            pct = (exit_price - self.open["entry"]) / self.open["entry"]
            if side == "SHORT":
                pct = -pct
            symbol = self.open["symbol"]
            self.open = None
            day_guard.on_trade_close(pct)
            return True, pct, symbol
        return False, None, None

    # ✅ force_close_position 要確定在 SimAdapter 類別內
    def force_close_position(self, symbol: str, reason="early_exit") -> Tuple[bool, Optional[float], Optional[float]]:
        """
        (模擬版) 立即市價平倉。
        回傳: (True/False, 近似 PnL 百分比, 近似出場價)
        """
        if not self.open or self.open.get("symbol") != symbol:
            print(f"Sim Warning: force_close_position called for {symbol} but no matching position found.")
            return False, None, None

        side = self.open["side"]
        entry = float(self.open["entry"])
        qty = self.open.get("qty", 0)

        approx_exit_price = entry
        try:
            approx_exit_price = self.best_price(symbol)
        except Exception:
            pass

        approx_pnl_pct = 0.0
        if entry > 0:
            pct = (approx_exit_price - entry) / entry
            if side == "SHORT":
                pct = -pct
            approx_pnl_pct = pct

        print(f"SIMULATED: Force closing {side} {symbol} Qty={qty} @ approx {approx_exit_price:.6f} (Reason: {reason})")
        self.open = None
        return True, approx_pnl_pct, approx_exit_price


class LiveAdapter:
    """
    Binance USDT-M Futures — 限價進場 + 兩條互斥條件單（TP/SL，closePosition=true）
    流程：
      1) LIMIT 進場（GTC），等待成交（逾時撤單）
      2) 成交後同時掛 TAKE_PROFIT_MARKET 與 STOP_MARKET（closePosition=true）
      3) 任一成交後撤另一單，回報 PnL%
    """
    def __init__(self):
        self.key = os.getenv("BINANCE_API_KEY", "")
        self.secret = os.getenv("BINANCE_SECRET", "")
        self.base = (BINANCE_FUTURES_TEST_BASE if USE_TESTNET else BINANCE_FUTURES_BASE)
        self.open = None  # {symbol, side, qty, entry, sl, tp, entryId, tpId, slId}

    def _sign(self, params: dict):
        q = "&".join([f"{k}={params[k]}" for k in sorted(params.keys())])
        sig = hmac.new(self.secret.encode(), q.encode(), hashlib.sha256).hexdigest()
        return q + "&signature=" + sig

    def balance_usdt(self) -> float:
        arr = self._get("/fapi/v2/balance", {})
        for a in arr:
            if a.get("asset") == "USDT":
                v = a.get("availableBalance") or a.get("balance") or "0"
                try:
                    return float(v)
                except Exception:
                    return 0.0
        return 0.0

    def _post(self, path, params):
        params = dict(params)
        params["timestamp"] = now_ts_ms() + int(TIME_OFFSET_MS)
        params.setdefault("recvWindow", 60000)
        qs = self._sign(params)
        r = SESSION.post(f"{self.base}{path}?{qs}",
                         headers={"X-MBX-APIKEY": self.key}, timeout=10)
        if not r.ok:
            print(f"[API ERROR] POST {path} returned {r.status_code}")
            print(f"Server msg: {r.text}")
        r.raise_for_status()
        return r.json()

    def _get(self, path, params):
        params = dict(params or {})
        params["timestamp"] = now_ts_ms() + int(TIME_OFFSET_MS)
        params.setdefault("recvWindow", 60000)
        qs = self._sign(params)
        r = SESSION.get(f"{self.base}{path}?{qs}",
                        headers={"X-MBX-APIKEY": self.key}, timeout=10)
        if not r.ok:
            print(f"[API ERROR] {path} returned {r.status_code}")
            print(f"Server msg: {r.text}")
        r.raise_for_status()
        return r.json()

    def _delete(self, path, params):
        params = dict(params or {})
        params["timestamp"] = now_ts_ms() + int(TIME_OFFSET_MS)
        params.setdefault("recvWindow", 60000)
        qs = self._sign(params)
        r = SESSION.delete(f"{self.base}{path}?{qs}",
                           headers={"X-MBX-APIKEY": self.key}, timeout=10)
        if not r.ok:
            print(f"[API ERROR] DELETE {path} returned {r.status_code}")
            print(f"Server msg: {r.text}")
        r.raise_for_status()
        return r.json()

    def has_open(self):
        return self.open is not None

    def best_price(self, symbol: str) -> float:
        if _ws_best_price:
            try:
                p = _ws_best_price(symbol)
                if p is not None:
                    return float(p)
            except Exception:
                pass
        data = _public_get_json("/fapi/v1/ticker/price",
                                params={"symbol": symbol}, timeout=5, tries=3)
        last = float(data.get("price"))
        return last  # ✅ 修正：不要再回 r.json()

    def place_bracket(self, symbol, side, qty, entry, sl, tp):
        if side not in ("LONG", "SHORT"):
            raise ValueError("side must be LONG/SHORT")
        order_side = "BUY" if side == "LONG" else "SELL"

        # 從緩存獲取該幣種的精度
        try:
            prec = EXCHANGE_INFO[symbol]
            qty_prec = prec['quantityPrecision']
            price_prec = prec['pricePrecision']
        except KeyError:
            qty_prec, price_prec = 0, 4  # Fallback

        entry_params = {
            "symbol": symbol,
            "side": order_side,
            "type": "LIMIT",
            "timeInForce": "GTC",
            "quantity": f"{qty:.{qty_prec}f}",
            "price": f"{entry:.{price_prec}f}",
            "newClientOrderId": f"entry_{int(time.time())}"
        }
        entry_res = self._post("/fapi/v1/order", entry_params)
        entry_id = entry_res["orderId"]

        # 2) 等待成交或逾時撤單
        t0 = time.time()
        filled = False
        while time.time() - t0 < ORDER_TIMEOUT_SEC:
            q = self._get("/fapi/v1/order", {"symbol": symbol, "orderId": entry_id})
            if q.get("status") == "FILLED":
                filled = True
                break
            time.sleep(0.6)
        if not filled:
            try:
                self._delete("/fapi/v1/order", {"symbol": symbol, "orderId": entry_id})
            finally:
                self.open = None
            raise TimeoutError("Entry limit order not filled within timeout; canceled.")

        # 3) 成交後掛 TP/SL
        exit_side = "SELL" if side == "LONG" else "BUY"
        tp_res = self._post("/fapi/v1/order", {
            "symbol": symbol,
            "side": exit_side,
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": f"{tp:.{price_prec}f}",
            "closePosition": "true",
            "workingType": "CONTRACT_PRICE"
        })
        sl_res = self._post("/fapi/v1/order", {
            "symbol": symbol,
            "side": exit_side,
            "type": "STOP_MARKET",
            "stopPrice": f"{sl:.{price_prec}f}",
            "closePosition": "true",
            "workingType": "CONTRACT_PRICE"
        })

        self.open = {
            "symbol": symbol, "side": side, "qty": qty,
            "entry": entry, "sl": sl, "tp": tp,
            "entryId": entry_id,
            "tpId": tp_res["orderId"], "slId": sl_res["orderId"]
        }
        return str(entry_id)

    def poll_and_close_if_hit(self, day_guard):
        if not self.open:
            return False, None, None
        symbol = self.open["symbol"]
        side = self.open["side"]
        entry = float(self.open["entry"])
        tpId = self.open["tpId"]
        slId = self.open["slId"]

        tp_q = self._get("/fapi/v1/order", {"symbol": symbol, "orderId": tpId})
        sl_q = self._get("/fapi/v1/order", {"symbol": symbol, "orderId": slId})
        tp_filled = tp_q.get("status") == "FILLED"
        sl_filled = sl_q.get("status") == "FILLED"

        if not tp_filled and not sl_filled:
            return False, None, None

        exit_price = float(self.open["tp"] if tp_filled else self.open["sl"])
        pct = (exit_price - entry) / entry
        if side == "SHORT":
            pct = -pct

        # 撤另一條未成交單
        try:
            other_id = slId if tp_filled else tpId
            other_q = self._get("/fapi/v1/order", {"symbol": symbol, "orderId": other_id})
            if other_q.get("status") in ("NEW", "PARTIALLY_FILLED"):
                self._delete("/fapi/v1/order", {"symbol": symbol, "orderId": other_id})
        except Exception:
            pass

        self.open = None
        day_guard.on_trade_close(pct)
        return True, pct, symbol

    def force_close_position(self, symbol: str, reason="early_exit") -> Tuple[bool, Optional[float], Optional[float]]:
        """
        立即以市價單平倉指定幣種，並嘗試記錄 PnL。
        回傳: (是否成功, 近似 PnL 百分比, 近似出場價)
        """
        if not self.open or self.open.get("symbol") != symbol:
            print(f"Warning: force_close_position called for {symbol} but no matching position found.")
            return False, None, None

        side = self.open["side"]
        qty = self.open["qty"]
        entry = float(self.open["entry"])
        close_side = "SELL" if side == "LONG" else "BUY"

        approx_exit_price = None
        try:
            approx_exit_price = self.best_price(symbol)
        except Exception as e:
            print(f"Warning: Could not get best price for {symbol} before force close: {e}")

        # 先撤 OCO 訂單
        try:
            self._delete("/fapi/v1/allOpenOrders", {"symbol": symbol})
            print(f"Successfully cancelled open orders for {symbol} before market close.")
        except Exception as e:
            print(f"ERROR: Failed to cancel OCO orders for {symbol}: {e}. Proceeding with market close attempt.")

        # 市價平倉
        try:
            close_params = {
                "symbol": symbol,
                "side": close_side,
                "type": "MARKET",
                "quantity": f"{qty}",
                "reduceOnly": "true"
            }
            close_res = self._post("/fapi/v1/order", close_params)
            print(f"Successfully sent MARKET close order for {symbol}: {close_res.get('orderId')}")

            approx_pnl_pct = None
            if approx_exit_price and entry > 0:
                pct = (approx_exit_price - entry) / entry
                if side == "SHORT":
                    pct = -pct
                approx_pnl_pct = pct

            self.open = None
            return True, approx_pnl_pct, approx_exit_price

        except Exception as e:
            msg = f"FATAL: Failed to send MARKET close order for {symbol}. Error: {e}"
            if hasattr(e, 'response') and e.response is not None:
                try:
                    msg += f" | Response: {e.response.status_code} {e.response.text}"
                except Exception:
                    pass
            print(msg)
            return False, None, None
