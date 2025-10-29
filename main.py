#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from datetime import datetime
from decimal import Decimal, InvalidOperation # <-- 保留 Decimal
import time, os
from dotenv import load_dotenv

from config import (USE_WEBSOCKET, USE_TESTNET, USE_LIVE, SCAN_INTERVAL_S, DAILY_TARGET_PCT, DAILY_LOSS_CAP,
                    PER_TRADE_RISK, SCAN_TOP_N, ALLOW_SHORT,
                    LARGE_TRADES_EARLY_EXIT_PCT, MIN_NOTIONAL_FALLBACK,
                    KLINE_INTERVAL, KLINE_LIMIT)
# (修改) 加入 fetch_klines 和 fetch_top_losers
from utils import fetch_top_gainers, fetch_top_losers, SESSION, fetch_klines
from risk_frame import DayGuard, position_size_notional, compute_bracket # (保留 ATR 版本)
from adapters import SimAdapter, LiveAdapter
# (修改) 匯入新的函數名稱
from signal_volume_breakout import calculate_vbo_long_signal, calculate_vbo_short_signal
from panel import live_render
from ws_client import start_ws, stop_ws
import threading
from journal import log_trade
import sys, threading, termios, tty, select, math
from utils import (load_exchange_info, EXCHANGE_INFO, update_time_offset, ws_best_price,
                   get_symbol_rule, floor_step_decimal, round_tick_decimal, to_decimal) # <-- 保留 Decimal 相關
from signal_large_trades_ws import large_trades_signal_ws, near_anchor_ok # <-- 保留大單訊號


def state_iter():
    # hotkeys local imports
    import sys, threading, termios, tty, select

    load_dotenv(override=True)
    # load_exchange_info() # <-- 移除這裡的呼叫, 由定時刷新處理

    day = DayGuard()
    adapter = LiveAdapter() if USE_LIVE else SimAdapter()

    # --- 獲取初始餘額 ---
    if USE_LIVE:
        try:
            equity = adapter.balance_usdt()
            print(f"--- Successfully fetched initial balance: {equity:.2f} USDT ---")
        except Exception as e:
            print(f"--- FATAL: Cannot fetch initial balance: {e} ---"); sys.exit(1)
    else:
        equity = float(os.getenv("EQUITY_USDT", "10000"))
        print(f"--- SIM Mode started with initial equity: {equity:.2f} USDT ---")

    start_equity = equity
    last_scan = 0
    last_time_sync = time.time()
    last_info_sync = 0.0 # 設為 0 以便啟動時立刻刷新
    prev_syms = []
    account = {"equity": equity, "balance": None, "testnet": USE_TESTNET}
    paused = {"scan": False}
    top_gainers_list = [] # 分開儲存
    top_losers_list = []  # 新增
    events = []
    position_view = None
    vbo_cache = {}
    COOLDOWN_SEC = 3
    REENTRY_BLOCK_SEC = 45
    cooldown = {"until": 0.0, "symbol_lock": {}}

    def log(msg, tag="SYS"):
        ts = datetime.now().strftime("%H:%M:%S")
        try: msg_str = str(msg)
        except Exception: msg_str = repr(msg)
        events.append((ts, f"{tag}: {msg_str}"))

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
                        if adapter.has_open() and adapter.open:
                            try:
                                log("Force close requested (Triggering now...)", "KEY")
                                sym_to_close = adapter.open["symbol"]
                                # 呼叫 force_close
                                close_success, _, _ = adapter.force_close_position(sym_to_close, reason="hotkey_x")
                                if not close_success:
                                     log(f"Force close failed via hotkey for {sym_to_close}", "ERROR")
                            except Exception as e: log(f"Force close error: {e}", "KEY")
                        else: log("No position to close", "KEY")
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

        # --- 定時刷新 Exchange Info ---
        if t_now - last_info_sync > 3600: # 每小時 (3600 秒)
             try:
                 load_exchange_info() # 強制刷新
                 last_info_sync = t_now
                 log("Exchange info refreshed periodically", "SYS")
             except Exception as e:
                 log(f"Periodic exchange info refresh failed: {e}", "ERROR")
                 last_info_sync = t_now

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
            closed = False
            pct = None
            sym = None
            early_exit_triggered = False

            # 1. 檢查提前出場訊號
            if adapter.open:
                try:
                    sym = adapter.open["symbol"]
                    side = adapter.open["side"]
                    lt = large_trades_signal_ws(sym) or {}
                    nowp = ws_best_price(sym)

                    if nowp:
                        nowp_float = float(nowp)
                        sell_rank = lt.get("sell_pct_rank")
                        buy_rank = lt.get("buy_pct_rank")

                        if side == "LONG" and sell_rank is not None and sell_rank >= LARGE_TRADES_EARLY_EXIT_PCT and near_anchor_ok(nowp_float, lt.get("sell_anchor")):
                            early_exit_triggered = True
                            exit_anchor = lt.get('sell_anchor')
                            log(f"LT Early Exit Triggered (Large Sell Rank={sell_rank:.1f}% >= {LARGE_TRADES_EARLY_EXIT_PCT}%)", sym)

                        elif side == "SHORT" and buy_rank is not None and buy_rank >= LARGE_TRADES_EARLY_EXIT_PCT and near_anchor_ok(nowp_float, lt.get("buy_anchor")):
                            early_exit_triggered = True
                            exit_anchor = lt.get('buy_anchor')
                            log(f"LT Early Exit Triggered (Large Buy Rank={buy_rank:.1f}% >= {LARGE_TRADES_EARLY_EXIT_PCT}%)", sym)

                    if early_exit_triggered:
                        close_success, approx_pnl, approx_exit = adapter.force_close_position(sym, reason="lt_early_exit")
                        if close_success:
                            closed = True
                            pct = approx_pnl if approx_pnl is not None else 0.0
                            # log(f"FORCE CLOSE {sym} Approx PnL={pct*100:.2f}% (Reason: lt_early_exit)", "TRADE") # 將在 3. 平倉後處理 統一 log
                            day.on_trade_close(pct)
                            position_view = None
                        else:
                            log(f"Force close attempt failed for {sym}, will retry poll.", "ERROR")
                            early_exit_triggered = False
                except Exception as ee:
                     log(f"Error during early exit check: {ee}", "ERROR")
                     early_exit_triggered = False
                     pass

            # 2. 如果沒有提前出場，才檢查 TP/SL
            if not closed and adapter.has_open():
                try:
                    closed, pct, sym = adapter.poll_and_close_if_hit(day)
                except Exception as e:
                    log(f"Poll error: {e}", "POLL")
                    closed, pct, sym = False, None, None

            # 3. 平倉後的處理
            if closed:
                log_message = f"CLOSE {sym} PnL={pct*100:.2f}% | Day={day.state.pnl_pct*100:.2f}%"
                if early_exit_triggered: log_message = f"FORCE {log_message}"
                log(log_message, "TRADE")

                cooldown["until"] = time.time() + COOLDOWN_SEC
                cooldown["symbol_lock"][sym] = time.time() + REENTRY_BLOCK_SEC

                # --- 更新權益 ---
                try:
                    if USE_LIVE:
                        equity = adapter.balance_usdt()
                        account["balance"] = equity
                        log(f"Balance updated: {equity:.2f}", "SYS")
                    else:
                        equity = start_equity * (1.0 + day.state.pnl_pct if day.state.pnl_pct is not None else 0.0)
                except Exception as e:
                    log(f"Balance update failed: {e}", "SYS")
                    equity = start_equity * (1.0 + day.state.pnl_pct if day.state.pnl_pct is not None else 0.0)

                position_view = None

                if early_exit_triggered:
                    log(f"Journal entry skipped for force close due to missing open data after close.", "WARN")
                    # TODO: Fix journaling for force close

        # --- 無持倉：掃描 & 進場 ---
        else:
            if not day.state.halted:
                # --- (修改) 掃描 & 更新 VBO 快取 (API 優化版) ---
                if not paused["scan"] and (t_now - last_scan > SCAN_INTERVAL_S):
                    try:
                        top_gainers_list = fetch_top_gainers(SCAN_TOP_N) # Step 1: Get Gainers (1 API call)
                        top_losers_list = fetch_top_losers(SCAN_TOP_N) if ALLOW_SHORT else [] # Step 2: Get Losers (1 API call)
                        last_scan = t_now

                        # 合併漲跌幅榜，去重，準備處理 VBO
                        all_symbols_to_check = {}
                        for (s, pct, last, vol) in top_gainers_list:
                            all_symbols_to_check[s] = (s, pct, last, vol)
                        for (s, pct, last, vol) in top_losers_list:
                            all_symbols_to_check[s] = (s, pct, last, vol)

                        new_cache = {}
                        symbols_for_ws = set()
                        symbols_processed_count = 0

                        # --- 先抓一次 K 線，再計算訊號（不再觸發「expected 4, got 2」） ---
                        for idx, (symbol, pct, last, vol) in enumerate(all_symbols_to_check.values()):
                            symbols_for_ws.add(symbol)

                            long_ok = False
                            short_ok = False
                            atr_value = None

                            try:
                                # 只打一次 REST
                                closes, highs, lows, vols = fetch_klines(symbol, KLINE_INTERVAL, KLINE_LIMIT)

                                # 訊號計算（沿用你現有函式）
                                long_ok, atr_long = calculate_vbo_long_signal(closes, highs, lows, vols)
                                short_ok, atr_short = (calculate_vbo_short_signal(closes, highs, lows, vols)
                                                       if ALLOW_SHORT else (False, None))

                                # 只保留有效 ATR
                                atr_value = atr_long if (atr_long is not None and atr_long > 0) \
                                           else (atr_short if (atr_short is not None and atr_short > 0) else None)

                                symbols_processed_count += 1

                            except Exception as fetch_e:
                                log(f"Error fetching/processing klines for {symbol}: {fetch_e}", "WARN")

                            new_cache[symbol] = {
                                "long": long_ok,
                                "short": short_ok,
                                "atr": atr_value
                            }

                            # 對每個 symbol 做輕微延遲（維持你原本節流）
                            time.sleep(0.3)

                        vbo_cache = new_cache
                        log(f"VBO cache updated for {symbols_processed_count}/{len(all_symbols_to_check)} symbols.", "SCAN")
                        # --- 核心修改結束 ---

                        # --- WebSocket 訂閱管理 ---
                        if USE_WEBSOCKET:
                            current_position_sym = None # 無持倉
                            # 訂閱 TopN 漲幅 + 跌幅
                            syms_to_subscribe_set = set(symbols_for_ws)
                            
                            current_set_to_subscribe = syms_to_subscribe_set
                            previous_subscribed_set = set(prev_syms)

                            symbols_changed_count = len(current_set_to_subscribe.symmetric_difference(previous_subscribed_set))
                            RELOAD_THRESHOLD = 5

                            needs_restart = False
                            if current_set_to_subscribe != previous_subscribed_set:
                                if not prev_syms:
                                     needs_restart = True
                                     print("DEBUG: First WebSocket subscription.")
                                elif symbols_changed_count > RELOAD_THRESHOLD:
                                     needs_restart = True
                                     print(f"DEBUG: Symbol set changed significantly ({symbols_changed_count} changes > {RELOAD_THRESHOLD}). Reloading WebSocket.")
                                     diff_added = current_set_to_subscribe - previous_subscribed_set
                                     diff_removed = previous_subscribed_set - current_set_to_subscribe
                                     if diff_added: print(f"DEBUG: Added symbols: {diff_added}")
                                     if diff_removed: print(f"DEBUG: Removed symbols: {diff_removed}")

                            if needs_restart:
                                syms_to_subscribe_list = sorted(list(current_set_to_subscribe))
                                start_ws(syms_to_subscribe_list, USE_TESTNET)
                                prev_syms = syms_to_subscribe_list

                    except Exception as e:
                        import traceback
                        traceback.print_exc()
                        log(f"Scan/Cache/WS error: {type(e).__name__} {e}", "SCAN")


                # --- 尋找進場候選 ---
                if t_now < cooldown["until"]:
                    candidate = None
                else:
                    candidate = None
                    nowp_cache = {}

                    # 1. 尋找 LONG 機會 (Gainers + VBO Long OR LT Long)
                    for s, pct, last, vol in top_gainers_list:
                        if t_now < cooldown['symbol_lock'].get(s, 0): continue

                        vbo_data = vbo_cache.get(s, {})
                        if not vbo_data: continue # VBO 快取可能還沒準備好或抓取失敗
                        
                        ok_vbo_long = vbo_data.get("long", False)
                        atr_value = vbo_data.get("atr")

                        lt = large_trades_signal_ws(s) or {}
                        ok_lt_long = False
                        nowp_float = None

                        if lt.get("buy_signal"):
                            try:
                                nowp = ws_best_price(s)
                                if nowp is not None: nowp_float = float(nowp)
                                else: nowp_float = float(last)
                                nowp_cache[s] = nowp_float
                                ok_lt_long = near_anchor_ok(nowp_float, lt.get("buy_anchor"))
                            except Exception as price_e:
                                 log(f"Error getting/parsing price for LT LONG check {s}: {price_e}", "WARN")
                                 nowp_cache[s] = float(last)

                        if (ok_vbo_long or ok_lt_long):
                            if atr_value is None or atr_value <= 0: continue
                            entry_price = float(nowp_cache.get(s, last) if ok_lt_long else last)
                            candidate = (s, entry_price, "LONG", atr_value)
                            log(f"Signal: LONG (VBO:{ok_vbo_long}, LT:{ok_lt_long}) ATR:{atr_value:.4g} @{entry_price:.6g}", s)
                            break

                    # 2. 如果沒找到 LONG，且允許 SHORT，尋找 SHORT 機會
                    if candidate is None and ALLOW_SHORT:
                        for s, pct, last, vol in top_losers_list:
                            if t_now < cooldown['symbol_lock'].get(s, 0): continue

                            vbo_data = vbo_cache.get(s, {})
                            if not vbo_data: continue
                            
                            ok_vbo_short = vbo_data.get("short", False)
                            atr_value = vbo_data.get("atr")

                            lt = large_trades_signal_ws(s) or {}
                            ok_lt_short = False
                            nowp_float = None

                            if lt.get("sell_signal"):
                                try:
                                    nowp = ws_best_price(s)
                                    if nowp is not None: nowp_float = float(nowp)
                                    else: nowp_float = float(last)
                                    nowp_cache[s] = nowp_float
                                    ok_lt_short = near_anchor_ok(nowp_float, lt.get("sell_anchor"))
                                except Exception as price_e:
                                    log(f"Error getting/parsing price for LT SHORT check {s}: {price_e}", "WARN")
                                    nowp_cache[s] = float(last)

                            if (ok_vbo_short or ok_lt_short):
                                if atr_value is None or atr_value <= 0: continue
                                entry_price = float(nowp_cache.get(s, last) if ok_lt_short else last)
                                candidate = (s, entry_price, "SHORT", atr_value)
                                log(f"Signal: SHORT (VBO:{ok_vbo_short}, LT:{ok_lt_short}) ATR:{atr_value:.4g} @{entry_price:.6g}", s)
                                break

                # --- 執行下單 (保留 Decimal 版本) ---
                if candidate:
                    symbol, entry, side, atr_for_trade = candidate
                    
                    tick_size_dec = None
                    step_size_dec = None
                    min_notional_rule = None
                    qty_prec = 0
                    price_prec = 4

                    try:
                        try:
                            prec = EXCHANGE_INFO[symbol]
                            qty_prec = prec['quantityPrecision']
                            price_prec = prec['pricePrecision']
                            tick_size_str = prec.get('tickSize')
                            step_size_str = prec.get('stepSize')
                            min_notional_rule = prec.get('minNotional')
                            if tick_size_str: tick_size_dec = to_decimal(tick_size_str)
                            if step_size_str: step_size_dec = to_decimal(step_size_str)
                            if min_notional_rule is not None and not isinstance(min_notional_rule, Decimal):
                                 min_notional_rule = to_decimal(min_notional_rule)
                        except KeyError:
                            log(f"No exchange info for {symbol}. Attempting live refresh...", "SYS")
                            load_exchange_info()
                            prec = EXCHANGE_INFO[symbol]
                            qty_prec = prec['quantityPrecision']
                            price_prec = prec['pricePrecision']
                            tick_size_str = prec.get('tickSize')
                            step_size_str = prec.get('stepSize')
                            min_notional_rule = prec.get('minNotional')
                            if tick_size_str: tick_size_dec = to_decimal(tick_size_str)
                            if step_size_str: step_size_dec = to_decimal(step_size_str)
                            if min_notional_rule is not None: min_notional_rule = to_decimal(min_notional_rule)
                            log(f"Successfully refreshed info for {symbol}", "SYS")
                    except KeyError:
                        log(f"Refresh failed. {symbol} not in official list. Using fallback guess.", "ERR")
                        qty_prec = 0
                        s_entry = f"{entry:.15f}"
                        if '.' in s_entry:
                             decimals = s_entry.split('.')[-1]
                             non_zero_idx = next((i for i, char in enumerate(decimals) if char != '0'), -1)
                             price_prec = (non_zero_idx + 3) if non_zero_idx != -1 else 4
                        else: price_prec = 0
                        price_prec = min(price_prec, 8)
                        log(f"Guessed price_prec={price_prec} for {symbol}", "SYS")
                        if price_prec > 0: tick_size_dec = Decimal('1e-' + str(price_prec))
                        if qty_prec == 0: step_size_dec = Decimal('1')
                        min_notional_rule = MIN_NOTIONAL_FALLBACK
                    except (InvalidOperation, TypeError, ValueError) as parse_e:
                        log(f"ORDER FAILED for {symbol}: Error parsing precision rules: {parse_e}", "ERROR")
                        continue

                    if atr_for_trade is None or atr_for_trade <= 0:
                        log(f"ORDER FAILED for {symbol}: Invalid ATR value {atr_for_trade} before pos sizing", "ERROR")
                        continue

                    notional = position_size_notional(equity, entry, atr_for_trade)
                    if notional <= 0:
                        log(f"Skipping {symbol}, calculated notional <= 0", "SYS")
                        continue

                    entry_dec = to_decimal(entry)
                    qty_dec = Decimal('NaN')
                    if entry_dec.is_finite() and entry_dec > Decimal(0) and step_size_dec is not None and step_size_dec.is_finite() and step_size_dec > Decimal(0):
                         qty_raw_dec = to_decimal(notional) / entry_dec
                         qty_dec = floor_step_decimal(qty_raw_dec, step_size_dec)
                    elif entry > 0:
                         qty_raw = notional / entry
                         qty_factor = 10**qty_prec
                         qty_f = math.floor(qty_raw * qty_factor) / qty_factor
                         qty_dec = to_decimal(qty_f)

                    if not qty_dec.is_finite() or qty_dec <= Decimal(0):
                        log(f"Skipping {symbol}, calculated qty is invalid or zero (QtyDec={qty_dec}, Notional={notional:.2f})", "SYS")
                        cooldown["until"] = time.time() + 1
                        continue

                    current_notional = qty_dec * entry_dec
                    min_notional_valid = min_notional_rule is not None and min_notional_rule.is_finite()
                    if min_notional_valid and current_notional < min_notional_rule:
                        log(f"Skipping {symbol}, Notional {current_notional:.2f} < MinNotional {min_notional_rule}", "SYS")
                        continue

                    sl_raw, tp_raw = compute_bracket(entry, side, atr_for_trade)
                    if sl_raw is None or tp_raw is None:
                        log(f"ORDER FAILED for {symbol}: Cannot compute SL/TP (ATR={atr_for_trade})", "ERROR")
                        continue

                    sl_dec = to_decimal(sl_raw)
                    tp_dec = to_decimal(tp_raw)
                    entry_fmt_dec = entry_dec

                    if tick_size_dec is not None and tick_size_dec.is_finite() and tick_size_dec > Decimal(0):
                         sl_dec = round_tick_decimal(sl_dec, tick_size_dec, direction=-1 if side=="LONG" else +1)
                         tp_dec = round_tick_decimal(tp_dec, tick_size_dec, direction=+1 if side=="LONG" else -1)
                         entry_fmt_dec = round_tick_decimal(entry_fmt_dec, tick_size_dec)
                    else:
                         sl_dec = to_decimal(round(sl_raw, price_prec))
                         tp_dec = to_decimal(round(tp_raw, price_prec))
                         entry_fmt_dec = to_decimal(round(entry, price_prec))

                    qty_final = float(qty_dec)
                    sl_final = float(sl_dec)
                    tp_final = float(tp_dec)
                    entry_fmt_final = float(entry_fmt_dec)

                    if not math.isfinite(qty_final) or qty_final <= 0 or \
                       not math.isfinite(sl_final) or not math.isfinite(tp_final) or \
                       not math.isfinite(entry_fmt_final):
                        log(f"ORDER FAILED for {symbol}: Final calculated values are invalid. Qty={qty_final}, SL={sl_final}, TP={tp_final}", "ERROR")
                        continue

                    try:
                        adapter.place_bracket(symbol, side, qty_final, entry_fmt_final, sl_final, tp_final)
                        position_view = {"symbol":symbol, "side":side, "qty":qty_final, "entry":entry_fmt_final, "sl":sl_final, "tp":tp_final}
                        log(f"OPEN {side} {symbol} Qty={qty_final:.{qty_prec}f} @{entry_fmt_final:.{price_prec}f} SL={sl_final:.{price_prec}f} TP={tp_final:.{price_prec}f}", "ORDER")
                        cooldown["until"] = time.time() + COOLDOWN_SEC
                    except Exception as e:
                        log(f"ORDER FAILED for {symbol}: {e}", "ERROR")
                        pass

        # --- 更新面板狀態 ---
        account["equity"] = equity
        if USE_LIVE and account.get("balance") is None:
            account["balance"] = equity

        yield {
            "top10": top_gainers_list, # 只顯示漲幅榜
            "day_state": day.state,
            "position": adapter.open if adapter.has_open() and adapter.open else position_view,
            "events": events,
            "account": account,
        }

        time.sleep(0.8)

# (移除 SIM state 相關函數)

# --- 主程式入口 ---
if __name__ == "__main__":
    # (移除 autosave worker 啟動)
    try:
        live_render(state_iter())
    except KeyboardInterrupt:
        print("\nCtrl+C detected. Exiting gracefully...")
    finally:
        # (移除 SIM state 強制儲存)
        try:
            stop_ws()
        except Exception:
            pass
        print("\n--- Bot stopped ---")
