#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from datetime import datetime
from decimal import Decimal, InvalidOperation # <-- 新增 Decimal
import time, os
from dotenv import load_dotenv

from config import (USE_WEBSOCKET, USE_TESTNET, USE_LIVE, SCAN_INTERVAL_S, DAILY_TARGET_PCT, DAILY_LOSS_CAP,
                    PER_TRADE_RISK, SCAN_TOP_N, ALLOW_SHORT,
                    LARGE_TRADES_EARLY_EXIT_PCT, MIN_NOTIONAL_FALLBACK) # <-- 匯入新設定
from utils import fetch_top_gainers, SESSION
from risk_frame import DayGuard, position_size_notional, compute_bracket # compute_bracket 現在需要 atr
from adapters import SimAdapter, LiveAdapter
from signal_volume_breakout import calculate_vbo_long_signal, calculate_vbo_short_signal # <-- 修改匯入
from panel import live_render
from ws_client import start_ws, stop_ws
import threading
from journal import log_trade
import sys, threading, termios, tty, select, math
from utils import (load_exchange_info, EXCHANGE_INFO, update_time_offset, ws_best_price,
                   get_symbol_rule, floor_step_decimal, round_tick_decimal, to_decimal) # <-- 匯入 Decimal 相關
from signal_large_trades_ws import large_trades_signal_ws, near_anchor_ok

def state_iter():
    # hotkeys local imports
    import sys, threading, termios, tty, select

    load_dotenv(override=True)

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
    last_info_sync = 0.0
    prev_syms = []
    account = {"equity": equity, "balance": None, "testnet": USE_TESTNET}
    paused = {"scan": False}
    top10 = []
    events = []
    position_view = None
    vbo_cache = {}
    COOLDOWN_SEC = 3
    REENTRY_BLOCK_SEC = 45
    cooldown = {"until": 0.0, "symbol_lock": {}}

    def log(msg, tag="SYS"):
        ts = datetime.now().strftime("%H:%M:%S")
        # Ensure msg is string
        try:
            msg_str = str(msg)
        except Exception:
            msg_str = repr(msg) # Fallback to repr
        events.append((ts, f"{tag}: {msg_str}"))


    # --- 鍵盤監聽 ---
    def _keyloop():
        # ... (鍵盤監聽邏輯不變) ...
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

        # --- 定時刷新 Exchange Info ---
        if t_now - last_info_sync > 3600:
             try:
                 load_exchange_info()
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
            try:
                closed, pct, sym = adapter.poll_and_close_if_hit(day)
            except Exception as e:
                log(f"Poll error: {e}", "POLL")
                closed, pct, sym = False, None, None

            # --- 檢查並執行反向大單提前出場 ---
            if not closed and adapter.has_open() and adapter.open:
                early_exit_triggered = False
                exit_reason = "lt_early_exit"
                exit_anchor = None
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
                        close_success, approx_pnl, approx_exit = adapter.force_close_position(sym, reason=exit_reason)
                        if close_success:
                            closed = True
                            pct = approx_pnl if approx_pnl is not None else 0.0
                            log(f"FORCE CLOSE {sym} Approx PnL={pct*100:.2f}% (Reason: {exit_reason})", "TRADE")
                            day.on_trade_close(pct)
                            position_view = None
                            cooldown["until"] = time.time() + COOLDOWN_SEC
                            cooldown["symbol_lock"][sym] = time.time() + REENTRY_BLOCK_SEC
                            log(f"Journal entry skipped for force close due to missing open data after close.", "WARN")
                            # TODO: Fix journaling for force close
                        else:
                            log(f"Force close attempt failed for {sym}, will retry poll.", "ERROR")
                except Exception as ee:
                     log(f"Error during early exit check: {ee}", "ERROR")
                     pass

            if closed:
                log(f"CLOSE {sym} PnL={pct*100:.2f}% | Day={day.state.pnl_pct*100:.2f}%", "TRADE") # Log PnL from poll/force close
                # Cooldown and lock already set if force closed, avoid double setting if polled
                if not early_exit_triggered: # Only set cooldown if closed by poll
                    cooldown["until"] = time.time() + COOLDOWN_SEC
                    cooldown["symbol_lock"][sym] = time.time() + REENTRY_BLOCK_SEC

                # --- 更新權益 ---
                try:
                    if USE_LIVE:
                        equity = adapter.balance_usdt()
                        account["balance"] = equity
                        log(f"Balance updated: {equity:.2f}", "SYS")
                    else:
                        equity = start_equity * (1.0 + day.state.pnl_pct) # Use day PnL for SIM equity approx
                except Exception as e:
                    log(f"Balance update failed: {e}", "SYS")
                    # Fallback for SIM equity calculation if needed
                    equity = start_equity * (1.0 + day.state.pnl_pct if day.state.pnl_pct is not None else 0.0)


                position_view = None

        # --- 無持倉：掃描 & 進場 ---
        else:
            if not day.state.halted:
                # --- 掃描 TopN & 快取 VBO (含 ATR) ---
                if not paused["scan"] and (t_now - last_scan > SCAN_INTERVAL_S):
                    try:
                        top10 = fetch_top_gainers(SCAN_TOP_N) # Step 1: Get TopN list (1 API call)
                        last_scan = t_now

                        new_cache = {}
                        symbols_for_ws = set()
                        symbols_processed_count = 0
                        
                        # --- 核心修改：先獲取 K 線，再計算訊號 ---
                        for s, pct, last, vol in top10:
                            symbols_for_ws.add(s)
                            klines_data = None
                            atr_value = None
                            long_ok = False
                            short_ok = False
                            
                            try:
                                # Step 2: Fetch K-lines ONCE per symbol (1 API call per symbol)
                                closes, highs, lows, vols = fetch_klines(s, KLINE_INTERVAL, KLINE_LIMIT)
                                klines_data = (closes, highs, lows, vols) # Store fetched data
                                
                                # Step 3: Calculate signals using the fetched data (NO API calls here)
                                long_ok, atr_long = calculate_vbo_long_signal(*klines_data)
                                short_ok, atr_short = calculate_vbo_short_signal(*klines_data) if ALLOW_SHORT else (False, None)
                                
                                atr_value = atr_long if atr_long is not None and atr_long > 0 else (atr_short if atr_short is not None and atr_short > 0 else None)
                                symbols_processed_count += 1
                                
                            except Exception as fetch_e:
                                 log(f"Error fetching/processing klines for {s}: {fetch_e}", "WARN")
                                 # Keep signals as False, atr as None if fetching failed

                            # Store results in cache
                            new_cache[s] = {
                                "long": long_ok,
                                "short": short_ok,
                                "atr": atr_value # Store None if calculation failed
                            }

                            # Step 4: Add delay AFTER processing each symbol
                            time.sleep(0.3) # <--- 保持延遲 (可酌情調整 0.3 ~ 0.5)

                        vbo_cache = new_cache # Update cache atomically
                        log(f"VBO cache updated for {symbols_processed_count}/{len(top10)} symbols.", "SCAN") # Log count
                        # --- 核心修改結束 ---

                        # --- WebSocket 訂閱管理 (邏輯不變) ---
                        if USE_WEBSOCKET:
                           # ... (省略 WebSocket 邏輯, 保持原樣) ...
                           # 1. 取得 TopN 幣種 Set (from symbols processed)
                           top_n_syms_set = symbols_for_ws
                           # 2. 取得當前持倉幣種
                           current_position_sym = None
                           if adapter.has_open() and adapter.open:
                               current_position_sym = adapter.open.get("symbol")
                           # 3. 合併得到需要訂閱的集合
                           syms_to_subscribe_set = set(top_n_syms_set) # Start with scanned symbols
                           if current_position_sym:
                               syms_to_subscribe_set.add(current_position_sym)
                           # 4. 比較與上次訂閱的集合
                           current_set_to_subscribe = syms_to_subscribe_set
                           previous_subscribed_set = set(prev_syms)
                           symbols_changed_count = len(current_set_to_subscribe.symmetric_difference(previous_subscribed_set))
                           RELOAD_THRESHOLD = 5
                           # 5. 判斷是否重啟
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
                           # 6. 執行重啟
                           if needs_restart:
                               syms_to_subscribe_list = sorted(list(current_set_to_subscribe)) # Sort for consistency
                               start_ws(syms_to_subscribe_list, USE_TESTNET)
                               prev_syms = syms_to_subscribe_list # Store the list that was actually subscribed

                    except Exception as e:
                        # Catch broader errors during scan/cache/ws block
                        log(f"Scan/Cache/WS error: {type(e).__name__} {e}", "SCAN")

                        # --- WebSocket 訂閱管理 ---
                        if USE_WEBSOCKET:
                            # 1. 取得 TopN 幣種 Set (from symbols processed)
                            top_n_syms_set = symbols_for_ws

                            # 2. 取得當前持倉幣種
                            current_position_sym = None
                            if adapter.has_open() and adapter.open:
                                current_position_sym = adapter.open.get("symbol")

                            # 3. 合併得到需要訂閱的集合
                            syms_to_subscribe_set = set(top_n_syms_set) # Start with scanned symbols
                            if current_position_sym:
                                syms_to_subscribe_set.add(current_position_sym)

                            # 4. 比較與上次訂閱的集合
                            current_set_to_subscribe = syms_to_subscribe_set
                            previous_subscribed_set = set(prev_syms)

                            symbols_changed_count = len(current_set_to_subscribe.symmetric_difference(previous_subscribed_set))
                            RELOAD_THRESHOLD = 5

                            # 5. 判斷是否重啟
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
                                # else: # Debug optional
                                #    print(f"DEBUG: Symbol set changed ({symbols_changed_count} <= {RELOAD_THRESHOLD}). WS not reloaded.")


                            # 6. 執行重啟
                            if needs_restart:
                                syms_to_subscribe_list = sorted(list(current_set_to_subscribe)) # Sort for consistency
                                start_ws(syms_to_subscribe_list, USE_TESTNET)
                                prev_syms = syms_to_subscribe_list # Store the list that was actually subscribed

                    except Exception as e:
                        # Catch broader errors during scan/cache/ws block
                        log(f"Scan/Cache/WS error: {type(e).__name__} {e}", "SCAN")


                # --- 尋找進場候選 ---
                if t_now < cooldown["until"]:
                    candidate = None
                else:
                    candidate = None
                    nowp_cache = {}

                    # Iterate through the fetched top10 list for potential candidates
                    for s, pct, last, vol in top10:
                        if t_now < cooldown['symbol_lock'].get(s, 0):
                            continue

                        vbo_data = vbo_cache.get(s, {}) # Get cached data for this symbol
                        ok_vbo_long = vbo_data.get("long", False)
                        ok_vbo_short = vbo_data.get("short", False)
                        atr_value = vbo_data.get("atr") # ATR from cache

                        # Check Large Trades signal (always do this regardless of VBO)
                        lt = large_trades_signal_ws(s) or {}
                        ok_lt_long = False
                        ok_lt_short = False

                        # Fetch current price once if needed for LT check
                        nowp_float = None
                        if lt.get("buy_signal") or (ALLOW_SHORT and lt.get("sell_signal")):
                            try:
                                nowp = ws_best_price(s)
                                if nowp is not None:
                                     nowp_float = float(nowp)
                                else:
                                     nowp_float = float(last) # Fallback to scan price
                                nowp_cache[s] = nowp_float # Cache it

                                if lt.get("buy_signal"):
                                    ok_lt_long = near_anchor_ok(nowp_float, lt.get("buy_anchor"))
                                if ALLOW_SHORT and lt.get("sell_signal"):
                                    ok_lt_short = near_anchor_ok(nowp_float, lt.get("sell_anchor"))
                            except (ValueError, TypeError) as price_e:
                                 log(f"Error getting/parsing price for LT check {s}: {price_e}", "WARN")
                                 nowp_cache[s] = float(last) # Fallback cache


                        # --- 整合訊號 (OR logic) ---
                        signal_side = None
                        signal_type = ""
                        entry_price = float(last) # Default to breakout price
                        selected_atr = atr_value # Use ATR from VBO cache

                        if (ok_vbo_long or ok_lt_long):
                            if selected_atr is not None and selected_atr > 0:
                                signal_side = "LONG"
                                signal_type = f"VBO:{ok_vbo_long}, LT:{ok_lt_long}"
                                if ok_lt_long: entry_price = float(nowp_cache.get(s, last)) # Use LT anchor price if LT triggered
                                log(f"Signal: {signal_side} ({signal_type}) ATR:{selected_atr:.4g} @{entry_price:.6g}", s)
                            else:
                                # log(f"Skipping LONG {s}: Invalid ATR {selected_atr}", "SIGNAL") # Debug
                                pass # Skip if ATR invalid

                        # Check SHORT only if LONG didn't trigger
                        elif ALLOW_SHORT and (ok_vbo_short or ok_lt_short):
                            if selected_atr is not None and selected_atr > 0:
                                signal_side = "SHORT"
                                signal_type = f"VBO:{ok_vbo_short}, LT:{ok_lt_short}"
                                if ok_lt_short: entry_price = float(nowp_cache.get(s, last)) # Use LT anchor price if LT triggered
                                log(f"Signal: {signal_side} ({signal_type}) ATR:{selected_atr:.4g} @{entry_price:.6g}", s)
                            else:
                                # log(f"Skipping SHORT {s}: Invalid ATR {selected_atr}", "SIGNAL") # Debug
                                pass # Skip if ATR invalid

                        # If a valid signal was found, set candidate and break
                        if signal_side:
                            candidate = (s, entry_price, signal_side, selected_atr)
                            break


                # --- 執行下單 ---
                if candidate:
                    symbol, entry, side, atr_for_trade = candidate

                    # --- 獲取精度規則 (含刷新與後備) ---
                    tick_size_dec = None
                    step_size_dec = None
                    min_notional_rule = None
                    qty_prec = 0 # Default if lookup fails badly
                    price_prec = 4 # Default if lookup fails badly

                    try:
                        try:
                            prec = EXCHANGE_INFO[symbol]
                            qty_prec = prec['quantityPrecision'] # int
                            price_prec = prec['pricePrecision'] # int
                            tick_size_str = prec.get('tickSize')
                            step_size_str = prec.get('stepSize')
                            min_notional_rule = prec.get('minNotional') # Decimal or None
                            if tick_size_str: tick_size_dec = to_decimal(tick_size_str)
                            if step_size_str: step_size_dec = to_decimal(step_size_str)
                            # Ensure min_notional is Decimal
                            if min_notional_rule is not None and not isinstance(min_notional_rule, Decimal):
                                 min_notional_rule = to_decimal(min_notional_rule)

                        except KeyError:
                            log(f"No exchange info for {symbol}. Attempting live refresh...", "SYS")
                            load_exchange_info() # Attempt refresh
                            # Retry getting rules after refresh
                            prec = EXCHANGE_INFO[symbol] # This will raise KeyError again if still not found
                            qty_prec = prec['quantityPrecision']
                            price_prec = prec['pricePrecision']
                            tick_size_str = prec.get('tickSize')
                            step_size_str = prec.get('stepSize')
                            min_notional_rule = prec.get('minNotional')
                            if tick_size_str: tick_size_dec = to_decimal(tick_size_str)
                            if step_size_str: step_size_dec = to_decimal(step_size_str)
                            if min_notional_rule is not None: min_notional_rule = to_decimal(min_notional_rule)
                            log(f"Successfully refreshed info for {symbol}", "SYS")

                    except KeyError: # Still not found after refresh
                        log(f"Refresh failed. {symbol} not in official list. Using fallback guess.", "ERR")
                        qty_prec = 0 # Fallback quantity precision
                        # Guess price precision based on entry price
                        s_entry = f"{entry:.15f}"
                        if '.' in s_entry:
                             decimals = s_entry.split('.')[-1]
                             non_zero_idx = next((i for i, char in enumerate(decimals) if char != '0'), -1)
                             price_prec = (non_zero_idx + 3) if non_zero_idx != -1 else 4
                        else: price_prec = 0
                        price_prec = min(price_prec, 8)
                        log(f"Guessed price_prec={price_prec} for {symbol}", "SYS")
                        # Guess tick/step based on price_prec for fallback rounding
                        if price_prec > 0: tick_size_dec = Decimal('1e-' + str(price_prec))
                        # Step size guess is harder, default to integer quantity if qty_prec is 0
                        if qty_prec == 0: step_size_dec = Decimal('1')
                        min_notional_rule = MIN_NOTIONAL_FALLBACK # Use fallback from config

                    except (InvalidOperation, TypeError, ValueError) as parse_e:
                        log(f"ORDER FAILED for {symbol}: Error parsing precision rules: {parse_e}", "ERROR")
                        continue # Skip order if rules cannot be parsed


                    # --- 計算數量 & SL/TP ---
                    if atr_for_trade is None or atr_for_trade <= 0:
                        log(f"ORDER FAILED for {symbol}: Invalid ATR value {atr_for_trade} before pos sizing", "ERROR")
                        continue

                    notional = position_size_notional(equity, entry, atr_for_trade)
                    if notional <= 0:
                        log(f"Skipping {symbol}, calculated notional <= 0", "SYS")
                        continue

                    # Use Decimal for quantity calculation if step_size is available
                    entry_dec = to_decimal(entry)
                    qty_dec = Decimal('NaN') # Initialize as NaN
                    if entry_dec.is_finite() and entry_dec > Decimal(0) and step_size_dec is not None and step_size_dec.is_finite() and step_size_dec > Decimal(0):
                         qty_raw_dec = to_decimal(notional) / entry_dec
                         qty_dec = floor_step_decimal(qty_raw_dec, step_size_dec)
                    elif entry > 0: # Fallback using float math if Decimal failed or step_size unknown
                         qty_raw = notional / entry
                         qty_factor = 10**qty_prec
                         qty_f = math.floor(qty_raw * qty_factor) / qty_factor
                         qty_dec = to_decimal(qty_f) # Convert float result back to Decimal if possible


                    if not qty_dec.is_finite() or qty_dec <= Decimal(0):
                        log(f"Skipping {symbol}, calculated qty is invalid or zero (QtyDec={qty_dec}, Notional={notional:.2f})", "SYS")
                        cooldown["until"] = time.time() + 1
                        continue

                    # --- Check minNotional ---
                    current_notional = qty_dec * entry_dec
                    # Ensure min_notional_rule is a valid Decimal before comparing
                    min_notional_valid = min_notional_rule is not None and min_notional_rule.is_finite()
                    if min_notional_valid and current_notional < min_notional_rule:
                        log(f"Skipping {symbol}, Notional {current_notional:.2f} < MinNotional {min_notional_rule}", "SYS")
                        continue

                    # --- Compute and round SL/TP ---
                    sl_raw, tp_raw = compute_bracket(entry, side, atr_for_trade)
                    if sl_raw is None or tp_raw is None:
                        log(f"ORDER FAILED for {symbol}: Cannot compute SL/TP (ATR={atr_for_trade})", "ERROR")
                        continue

                    # Round using Decimal if tick_size is available
                    sl_dec = to_decimal(sl_raw)
                    tp_dec = to_decimal(tp_raw)
                    entry_fmt_dec = entry_dec # Use unrounded entry for Decimal rounding

                    if tick_size_dec is not None and tick_size_dec.is_finite() and tick_size_dec > Decimal(0):
                         sl_dec = round_tick_decimal(sl_dec, tick_size_dec, direction=-1 if side=="LONG" else +1)
                         tp_dec = round_tick_decimal(tp_dec, tick_size_dec, direction=+1 if side=="LONG" else -1)
                         entry_fmt_dec = round_tick_decimal(entry_fmt_dec, tick_size_dec) # Round entry too for consistency
                    else: # Fallback using float round
                         sl_dec = to_decimal(round(sl_raw, price_prec))
                         tp_dec = to_decimal(round(tp_raw, price_prec))
                         entry_fmt_dec = to_decimal(round(entry, price_prec))

                    # Convert final values back to float for adapter
                    qty_final = float(qty_dec)
                    sl_final = float(sl_dec)
                    tp_final = float(tp_dec)
                    entry_fmt_final = float(entry_fmt_dec)

                    # Final check: Ensure SL/TP are valid numbers before placing order
                    if not math.isfinite(qty_final) or qty_final <= 0 or \
                       not math.isfinite(sl_final) or not math.isfinite(tp_final) or \
                       not math.isfinite(entry_fmt_final):
                        log(f"ORDER FAILED for {symbol}: Final calculated values are invalid. Qty={qty_final}, SL={sl_final}, TP={tp_final}", "ERROR")
                        continue


                    # --- 下單 (含錯誤捕捉) ---
                    try:
                        adapter.place_bracket(symbol, side, qty_final, entry_fmt_final, sl_final, tp_final)
                        position_view = {"symbol":symbol, "side":side, "qty":qty_final, "entry":entry_fmt_final, "sl":sl_final, "tp":tp_final}
                        # Use precise formatting based on qty_prec/price_prec for logging
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
            "top10": top10,
            "day_state": day.state,
            "position": adapter.open if adapter.has_open() and adapter.open else position_view,
            "events": events,
            "account": account,
        }

        time.sleep(0.8)

# --- 主程式入口 ---
if __name__ == "__main__":
    try:
        live_render(state_iter())
    except KeyboardInterrupt: # 優雅處理 Ctrl+C
        print("\nCtrl+C detected. Exiting gracefully...")
    finally:
        try:
            stop_ws()
        except Exception:
            pass
        print("\n--- Bot stopped ---")
