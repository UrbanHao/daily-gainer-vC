#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from datetime import datetime
import time, os
from dotenv import load_dotenv

from config import (USE_WEBSOCKET, USE_TESTNET, USE_LIVE, SCAN_INTERVAL_S, DAILY_TARGET_PCT, DAILY_LOSS_CAP,
                    PER_TRADE_RISK, SCAN_TOP_N, ALLOW_SHORT) # <-- 匯入 ALLOW_SHORT
from utils import fetch_top_gainers, SESSION
from risk_frame import DayGuard, position_size_notional, compute_bracket # compute_bracket 現在需要 atr
from adapters import SimAdapter, LiveAdapter
from signal_volume_breakout import get_vbo_long_signal, get_vbo_short_signal # <-- 修改匯入
from panel import live_render
from ws_client import start_ws, stop_ws
import threading
from journal import log_trade
import sys, threading, termios, tty, select, math
from utils import load_exchange_info, EXCHANGE_INFO, update_time_offset, ws_best_price
from signal_large_trades_ws import large_trades_signal_ws, near_anchor_ok

def state_iter():
    # hotkeys local imports
    import sys, threading, termios, tty, select

    load_dotenv(override=True)
    load_exchange_info() # 啟動時載入精度規則

    day = DayGuard()
    adapter = LiveAdapter() if USE_LIVE else SimAdapter()

    # --- 獲取初始餘額 ---
    if USE_LIVE:
        try:
            equity = adapter.balance_usdt()
            print(f"--- Successfully fetched initial balance: {equity:.2f} USDT ---")
        except Exception as e:
            print(f"--- FATAL: Cannot fetch initial balance: {e} ---")
            print("Check API Key permissions or .env settings. Exiting.")
            sys.exit(1)
    else:
        equity = float(os.getenv("EQUITY_USDT", "10000"))
        print(f"--- SIM Mode started with initial equity: {equity:.2f} USDT ---")

    start_equity = equity
    last_scan = 0
    last_time_sync = time.time()
    prev_syms = [] # <--- 用來儲存上次的幣種列表 (List)
    account = {"equity": equity, "balance": None, "testnet": USE_TESTNET}
    paused = {"scan": False}
    top10 = []
    events = []
    position_view = None
    vbo_cache = {} # VBO 快取 (儲存 {"long": bool, "short": bool, "atr": float})
    COOLDOWN_SEC = 3
    REENTRY_BLOCK_SEC = 45
    cooldown = {"until": 0.0, "symbol_lock": {}}

    def log(msg, tag="SYS"):
        ts = datetime.now().strftime("%H:%M:%S")
        events.append((ts, f"{tag}: {msg}"))

    # --- 鍵盤監聽 ---
    def _keyloop():
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while True:
                r,_,_ = select.select([sys.stdin],[],[],0.05)
                if r:
                    ch = sys.stdin.read(1)
                    if ch == "p":
                        paused["scan"] = not paused["scan"]
                        log(f"Scan toggled -> {paused['scan']}", "KEY")
                    elif ch == "x":
                        if adapter.has_open():
                            try:
                                log("Force close requested (Not implemented yet)", "KEY")
                            except Exception as e:
                                log(f"Close error: {e}", "KEY")
                        else:
                            log("No position to close", "KEY")
                    elif ch == "!":
                        day.state.halted = True
                        log("Manual HALT for today", "KEY")
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    threading.Thread(target=_keyloop, daemon=True).start()

    # --- 主迴圈 ---
    while True:
        t_now = time.time()
        day.rollover()

        # --- 時間校正 ---
        if t_now - last_time_sync > 1800:
            try:
                new_offset = update_time_offset()
                log(f"Time offset re-synced: {new_offset} ms", "SYS")
                last_time_sync = t_now
            except Exception as e:
                log(f"Time offset sync failed: {e}", "ERROR")
                last_time_sync = t_now

        # --- 持倉管理 ---
        if adapter.has_open():
            try:
                closed, pct, sym = adapter.poll_and_close_if_hit(day)
            except Exception as e:
                log(f"Poll error: {e}", "POLL")
                closed, pct, sym = False, None, None

            # --- 檢查反向大單 ---
            if not closed and adapter.open:
                try:
                    sym = adapter.open["symbol"]
                    side = adapter.open["side"]
                    lt = large_trades_signal_ws(sym) or {}
                    nowp = ws_best_price(sym)
                    if nowp:
                        nowp_float = float(nowp)
                        if side == "LONG" and lt.get("sell_signal") and near_anchor_ok(nowp_float, lt.get("sell_anchor")):
                            log(f"LT Early Exit Signal (Large Sell @ {lt.get('sell_anchor'):.4f})", sym)
                        elif side == "SHORT" and lt.get("buy_signal") and near_anchor_ok(nowp_float, lt.get("buy_anchor")):
                            log(f"LT Early Exit Signal (Large Buy @ {lt.get('buy_anchor'):.4f})", sym)
                except Exception:
                    pass

            if closed:
                log(f"CLOSE {sym} PnL={pct*100:.2f}% | Day={day.state.pnl_pct*100:.2f}%", "TRADE")
                cooldown["until"] = time.time() + COOLDOWN_SEC
                cooldown["symbol_lock"][sym] = time.time() + REENTRY_BLOCK_SEC

                # --- 更新權益 ---
                try:
                    if USE_LIVE:
                        equity = adapter.balance_usdt()
                        account["balance"] = equity
                        log(f"Balance updated: {equity:.2f}", "SYS")
                    else:
                        equity = start_equity * (1.0 + day.state.pnl_pct)
                except Exception as e:
                    log(f"Balance update failed: {e}", "SYS")
                    equity = start_equity * (1.0 + day.state.pnl_pct)

                position_view = None

        # --- 無持倉：掃描 & 進場 ---
        else:
            if not day.state.halted:
                # --- 掃描 TopN & 快取 VBO (含 ATR) ---
                if not paused["scan"] and (t_now - last_scan > SCAN_INTERVAL_S):
                    try:
                        top10 = fetch_top_gainers(SCAN_TOP_N)
                        last_scan = t_now

                        new_cache = {}
                        for s, pct, last, vol in top10:
                            long_ok, atr_long = get_vbo_long_signal(s)
                            short_ok, atr_short = get_vbo_short_signal(s) if ALLOW_SHORT else (False, None)
                            new_cache[s] = {
                                "long": long_ok,
                                "short": short_ok,
                                "atr": atr_long if atr_long is not None and atr_long > 0 else (atr_short if atr_short is not None and atr_short > 0 else None)
                            }
                        vbo_cache = new_cache

                        log(f"Scan top{SCAN_TOP_N} OK, VBO cache updated", "SCAN")

                        if USE_WEBSOCKET:
                            syms = [t[0] for t in top10]
                            current_set = set(syms)
                            previous_set = set(prev_syms)

                            # --- 修改：只有當差異超過 N 個幣時才重啟 ---
                            symbols_changed_count = len(current_set.symmetric_difference(previous_set))
                            RELOAD_THRESHOLD = 5 # 例如：差異超過 5 個幣才重啟

                            if current_set != previous_set: # 只有在集合真的改變時才檢查
                                if symbols_changed_count > RELOAD_THRESHOLD or not prev_syms: # 首次啟動也觸發
                                    print(f"DEBUG: Symbol set changed significantly ({symbols_changed_count} changes > {RELOAD_THRESHOLD}). Reloading WebSocket.")
                                    # --- Debugging ---
                                    diff_added = current_set - previous_set
                                    diff_removed = previous_set - current_set
                                    if diff_added: print(f"DEBUG: Added symbols: {diff_added}")
                                    if diff_removed: print(f"DEBUG: Removed symbols: {diff_removed}")
                                    # --- End Debugging ---
                                    start_ws(syms, USE_TESTNET)
                                    prev_syms = syms # 更新 prev_syms 列表
                                # else: # Debugging (Optional):
                                #     print(f"DEBUG: Symbol set changed ({symbols_changed_count} changes <= {RELOAD_THRESHOLD}). WS not reloaded.")

                    except Exception as e:
                        log(f"Scan/Cache error: {e}", "SCAN")


                # --- 尋找進場候選 ---
                if t_now < cooldown["until"]:
                    candidate = None
                else:
                    candidate = None
                    nowp_cache = {}

                    for s, pct, last, vol in top10:
                        if t_now < cooldown['symbol_lock'].get(s, 0):
                            continue

                        vbo_data = vbo_cache.get(s, {})
                        ok_vbo_long = vbo_data.get("long", False)
                        ok_vbo_short = vbo_data.get("short", False)
                        atr_value = vbo_data.get("atr")

                        lt = large_trades_signal_ws(s) or {}
                        ok_lt_long = False
                        ok_lt_short = False

                        if lt.get("buy_signal") or lt.get("sell_signal"):
                            try:
                                nowp = ws_best_price(s)
                                if nowp is None: nowp = last
                                nowp_float = float(nowp)
                                nowp_cache[s] = nowp_float

                                if lt.get("buy_signal"):
                                    ok_lt_long = near_anchor_ok(nowp_float, lt.get("buy_anchor"))
                                if ALLOW_SHORT and lt.get("sell_signal"):
                                    ok_lt_short = near_anchor_ok(nowp_float, lt.get("sell_anchor"))
                            except Exception:
                                nowp_cache[s] = last

                        # --- 整合訊號 (加入 ATR 檢查) ---
                        if (ok_vbo_long or ok_lt_long):
                            if atr_value is None or atr_value <= 0:
                                continue
                            entry_price = float(nowp_cache.get(s, last) if ok_lt_long else last)
                            candidate = (s, entry_price, "LONG", atr_value)
                            log(f"Signal: LONG (VBO:{ok_vbo_long}, LT:{ok_lt_long}) ATR:{atr_value:.4g} @{entry_price:.6g}", s)
                            break

                        if ALLOW_SHORT and (ok_vbo_short or ok_lt_short):
                            if atr_value is None or atr_value <= 0:
                                continue
                            entry_price = float(nowp_cache.get(s, last) if ok_lt_short else last)
                            candidate = (s, entry_price, "SHORT", atr_value)
                            log(f"Signal: SHORT (VBO:{ok_vbo_short}, LT:{ok_lt_short}) ATR:{atr_value:.4g} @{entry_price:.6g}", s)
                            break

                # --- 執行下單 ---
                if candidate:
                    symbol, entry, side, atr_for_trade = candidate

                    # --- 計算精度 ---
                    try:
                        prec = EXCHANGE_INFO[symbol]
                        qty_prec = prec['quantityPrecision']
                        price_prec = prec['pricePrecision']
                    except KeyError:
                        log(f"No exchange info for {symbol}. Attempting live refresh...", "SYS")
                        try:
                            load_exchange_info()
                            prec = EXCHANGE_INFO[symbol]
                            qty_prec = prec['quantityPrecision']
                            price_prec = prec['pricePrecision']
                            log(f"Successfully refreshed info for {symbol}", "SYS")
                        except KeyError:
                            log(f"Refresh failed. {symbol} not in official list. Using fallback guess.", "ERR")
                            qty_prec = 0
                            s_entry = f"{entry:.15f}"
                            if '.' in s_entry:
                                decimals = s_entry.split('.')[-1]
                                non_zero_idx = -1
                                for i, char in enumerate(decimals):
                                    if char != '0': non_zero_idx = i; break
                                price_prec = (non_zero_idx + 3) if non_zero_idx != -1 else 4
                            else: price_prec = 0
                            price_prec = min(price_prec, 8)
                            log(f"Guessed price_prec={price_prec} for {symbol}", "SYS")

                    # --- 計算數量 & SL/TP ---
                    if atr_for_trade is None or atr_for_trade <= 0:
                        log(f"ORDER FAILED for {symbol}: Invalid ATR value {atr_for_trade} before pos sizing", "ERROR")
                        continue

                    notional = position_size_notional(equity, entry, atr_for_trade)
                    if notional <= 0:
                        log(f"Skipping {symbol}, calculated notional <= 0", "SYS")
                        continue

                    qty_raw = notional / entry
                    qty_factor = 10**qty_prec
                    qty = math.floor(qty_raw * qty_factor) / qty_factor

                    if qty <= 0.0:
                        log(f"Skipping {symbol}, calculated qty <= 0 (Notional={notional:.2f})", "SYS")
                        cooldown["until"] = time.time() + 1
                        continue

                    sl_raw, tp_raw = compute_bracket(entry, side, atr_for_trade)
                    if sl_raw is None or tp_raw is None:
                        log(f"ORDER FAILED for {symbol}: Cannot compute SL/TP (ATR={atr_for_trade})", "ERROR")
                        continue

                    sl = round(sl_raw, price_prec)
                    tp = round(tp_raw, price_prec)
                    entry_fmt = round(entry, price_prec)

                    # --- 下單 (含錯誤捕捉) ---
                    try:
                        adapter.place_bracket(symbol, side, qty, entry_fmt, sl, tp)
                        position_view = {"symbol":symbol, "side":side, "qty":qty, "entry":entry_fmt, "sl":sl, "tp":tp}
                        log(f"OPEN {side} {symbol} Qty={qty:.{qty_prec}f} @{entry_fmt:.{price_prec}f} SL={sl:.{price_prec}f} TP={tp:.{price_prec}f}", "ORDER")
                        cooldown["until"] = time.time() + COOLDOWN_SEC
                    except Exception as e:
                        log(f"ORDER FAILED for {symbol}: {e}", "ERROR")
                        pass

        # --- 更新面板狀態 ---
        account["equity"] = equity
        if USE_LIVE and account.get("balance") is None:
            account["balance"] = equity

        yield {
            "top10": top10,
            "day_state": day.state,
            "position": adapter.open if adapter.has_open() else position_view,
            "events": events,
            "account": account,
        }

        time.sleep(0.8)

# --- 主程式入口 ---
if __name__ == "__main__":
    try:
        live_render(state_iter())
    finally:
        try:
            stop_ws()
        except Exception:
            pass
        print("\n--- Bot stopped ---")
