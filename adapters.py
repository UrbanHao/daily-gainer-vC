import os, hmac, hashlib, requests, time
from typing import Tuple, Optional
from utils import now_ts_ms, SESSION, BINANCE_FUTURES_BASE, TIME_OFFSET_MS, ws_best_price, EXCHANGE_INFO
try:
    TIME_OFFSET_MS
except NameError:
    TIME_OFFSET_MS = 0  # fallback if not imported
from dotenv import dotenv_values
import os
try:
    from ws_client import ws_best_price as _ws_best_price
except Exception:
    _ws_best_price = None
from config import USE_TESTNET, ORDER_TIMEOUT_SEC

class SimAdapter:
    def __init__(self):
        self.open = None
    def has_open(self): return self.open is not None
    def best_price(self, symbol: str) -> float:
        # 先試 WS
        if _ws_best_price:
            try:
                p = _ws_best_price(symbol)
                if p is not None:
                    return float(p)
            except Exception:
                pass
        # 後備：REST
        base = getattr(self, "base", BINANCE_FUTURES_BASE)
        r = SESSION.get(f"{base}/fapi/v1/ticker/price", params={"symbol": symbol}, timeout=5)
        r.raise_for_status()
        return float(r.json()["price"])
    def place_bracket(self, symbol, side, qty, entry, sl, tp):
        self.open = {"symbol":symbol, "side":side, "qty":qty, "entry":entry, "sl":sl, "tp":tp}
        return "SIM-ORDER"
    def poll_and_close_if_hit(self, day_guard):
        if not self.open: return False, None, None
        try:
            p = self.best_price(self.open["symbol"])
        except Exception as _e:
            return False, None, None
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
# --- (新) 加入 SimAdapter 正確的 force_close_position ---
    def force_close_position(self, symbol: str, reason="early_exit") -> Tuple[bool, Optional[float], Optional[float]]:
        """
        (模擬版本) 模擬立即市價平倉。
        回傳: (永遠為 True, 近似 PnL 百分比, 近似出場價)
        """
        if not self.open or self.open.get("symbol") != symbol:
            print(f"Sim Warning: force_close_position called for {symbol} but no matching position found.")
            return False, None, None

        side = self.open["side"]
        entry = float(self.open["entry"])
        qty = self.open.get("qty", 0)

        approx_exit_price = entry # 模擬簡單平倉在入場價 (PnL=0)
        try:
            approx_exit_price = self.best_price(symbol) # 嘗試獲取市價
        except Exception:
            pass # 獲取失敗就用 entry

        approx_pnl_pct = 0.0
        if entry > 0:
            pct = (approx_exit_price - entry) / entry
            if side == "SHORT": pct = -pct
            approx_pnl_pct = pct

        print(f"SIMULATED: Force closing {side} {symbol} Qty={qty} @ approx {approx_exit_price:.6f} (Reason: {reason})")

        self.open = None # 清除狀態

        # main.py 會處理 day_guard 和 journal
        return True, approx_pnl_pct, approx_exit_price
    # --- SimAdapter 的 force_close_position 結束 ---
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

    def _sign(self, params:dict):
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
            r = SESSION.post(f"{self.base}{path}?{qs}", headers={"X-MBX-APIKEY": self.key}, timeout=10)
            
            # --- 新增除錯訊息 ---
            if not r.ok:
                print(f"[API ERROR] POST {path} returned {r.status_code}")
                print(f"Server msg: {r.text}")
            # ------------------

            r.raise_for_status()
            return r.json()

    def _get(self, path, params):
            params = dict(params or {})
            params["timestamp"] = now_ts_ms() + int(TIME_OFFSET_MS)
            params.setdefault("recvWindow", 60000)

            qs = self._sign(params)
            r = SESSION.get(f"{self.base}{path}?{qs}", headers={"X-MBX-APIKEY": self.key}, timeout=10)
            
            # --- 新增除錯訊息 ---
            if not r.ok:
                print(f"[API ERROR] {path} returned {r.status_code}")
                print(f"Server msg: {r.text}")
            # ------------------
            
            r.raise_for_status()
            return r.json()

    def _delete(self, path, params):
            params = dict(params or {})
            params["timestamp"] = now_ts_ms() + int(TIME_OFFSET_MS)
            params.setdefault("recvWindow", 60000)
            qs = self._sign(params)
            r = SESSION.delete(f"{self.base}{path}?{qs}", headers={"X-MBX-APIKEY": self.key}, timeout=10)
            
            # --- 新增除錯訊息 ---
            if not r.ok:
                print(f"[API ERROR] DELETE {path} returned {r.status_code}")
                print(f"Server msg: {r.text}")
            # ------------------

            r.raise_for_status()
            return r.json()

    def has_open(self): return self.open is not None

    def best_price(self, symbol: str) -> float:
        if _ws_best_price:
            try:
                p = _ws_best_price(symbol)
                if p is not None:
                    return float(p)
            except Exception:
                pass
        r = SESSION.get(f"{BINANCE_FUTURES_BASE}/fapi/v1/ticker/price", params={"symbol": symbol}, timeout=5)
        r.raise_for_status()
        return float(r.json()["price"])

    def place_bracket(self, symbol, side, qty, entry, sl, tp):
        if side not in ("LONG","SHORT"):
            raise ValueError("side must be LONG/SHORT")
        order_side = "BUY" if side=="LONG" else "SELL"

        # --- 修正：使用 f-string 精度格式化 ---
        try:
            # 從緩存獲取該幣種的精度
            prec = EXCHANGE_INFO[symbol]
            qty_prec = prec['quantityPrecision']
            price_prec = prec['pricePrecision']
        except KeyError:
            qty_prec, price_prec = 0, 4 # Fallback

        entry_params = {
            "symbol": symbol,
            "side": order_side,
            "type": "LIMIT",
            "timeInForce": "GTC",
            "quantity": f"{qty:.{qty_prec}f}",  # <--- 修改點
            "price": f"{entry:.{price_prec}f}", # <--- 修改點
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

        # 3) 成交後掛 TP/SL — closePosition(true) 等同 reduceOnly 全倉
        exit_side = "SELL" if side=="LONG" else "BUY"
        tp_res = self._post("/fapi/v1/order", {
            "symbol": symbol,
            "side": exit_side,
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": f"{tp:.{price_prec}f}", # <--- 修改點
            "closePosition": "true",
            "workingType": "CONTRACT_PRICE"
        })
        sl_res = self._post("/fapi/v1/order", {
            "symbol": symbol,
            "side": exit_side,
            "type": "STOP_MARKET",
            "stopPrice": f"{sl:.{price_prec}f}", # <--- 修改點
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

        # 撤另一條未成交單
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
    def force_close_position(self, symbol: str, reason="early_exit") -> Tuple[bool, Optional[float], Optional[float]]:
        """
        (新) 立即以市價單平倉指定幣種，並嘗試記錄 PnL。
        回傳: (是否成功發送平倉單, 近似 PnL 百分比, 近似出場價)
        """
        if not self.open or self.open.get("symbol") != symbol:
            print(f"Warning: force_close_position called for {symbol} but no matching position found.")
            return False, None, None

        side = self.open["side"]
        qty = self.open["qty"]
        entry = float(self.open["entry"])
        tpId = self.open.get("tpId")
        slId = self.open.get("slId")
        close_side = "SELL" if side == "LONG" else "BUY"

        # 1. 立即獲取當前價格作為近似出場價
        approx_exit_price = None
        try:
            approx_exit_price = self.best_price(symbol)
        except Exception as e:
            print(f"Warning: Could not get best price for {symbol} before force close: {e}")

        # 2. (重要) 先嘗試撤銷 OCO 訂單
        cancelled_oco = False
        try:
            self._delete("/fapi/v1/allOpenOrders", {"symbol": symbol})
            print(f"Successfully cancelled open orders for {symbol} before market close.")
            cancelled_oco = True
        except Exception as e:
            print(f"ERROR: Failed to cancel OCO orders for {symbol}: {e}. Proceeding with market close attempt.")

        # 3. 發送市價平倉單 (reduceOnly 確保只平倉)
        try:
            close_params = {
                "symbol": symbol,
                "side": close_side,
                "type": "MARKET",
                "quantity": f"{qty}", # 使用原始開倉數量
                "reduceOnly": "true"
            }
            close_res = self._post("/fapi/v1/order", close_params)
            print(f"Successfully sent MARKET close order for {symbol}: {close_res.get('orderId')}")

            # 4. 計算近似 PnL
            approx_pnl_pct = None
            if approx_exit_price:
                pct = (approx_exit_price - entry) / entry if entry > 0 else 0
                if side == "SHORT": pct = -pct
                approx_pnl_pct = pct

            # 5. 清除內部狀態 (無論 PnL 是否算成功)
            self.open = None
            return True, approx_pnl_pct, approx_exit_price

        except Exception as e:
            # --- 以下是 except 區塊，必須縮排 ---
            error_message = f"FATAL: Failed to send MARKET close order for {symbol}. Error: {e}"
            if hasattr(e, 'response') and e.response is not None:
                try:
                    error_message += f" | Response: {e.response.status_code} {e.response.text}"
                except Exception:
                    pass
            print(error_message)
            # --- 縮排結束 ---

            # 平倉失敗，保持 self.open 狀態不變
            return False, None, None
