#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from datetime import datetime
import time, os
from dotenv import load_dotenv

from config import (USE_WEBSOCKET, USE_TESTNET, USE_LIVE, SCAN_INTERVAL_S, DAILY_TARGET_PCT, DAILY_LOSS_CAP,
                    PER_TRADE_RISK)
from utils import fetch_top_gainers, SESSION
from risk_frame import DayGuard, position_size_notional, compute_bracket
from adapters import SimAdapter, LiveAdapter
from signal_volume_breakout import volume_breakout_ok
from panel import live_render
from ws_client import start_ws, stop_ws
import threading
from journal import log_trade
import sys, threading, termios, tty, select

def state_iter():
    # hotkeys local imports (ensure available even if top-level imports failed)
    import sys, threading, termios, tty, select  # hotkeys

    load_dotenv(override=True)
    equity = float(os.getenv("EQUITY_USDT", "10000"))

    day = DayGuard()
    adapter = LiveAdapter() if USE_LIVE else SimAdapter()

    last_scan = 0
    prev_syms = []
    last_bal_ts = 0.0
    account = {"equity": equity, "balance": None, "testnet": USE_TESTNET}
    paused = {"scan": False}
    top10 = []
    events = []
    position_view = None

    def log(msg, tag="SYS"):
        ts = datetime.now().strftime("%H:%M:%S")
        events.append((ts, f"{tag}: {msg}"))
    # --- 非阻塞鍵盤監聽（p: 暫停/恢復掃描, x: 立即平倉, !: 今日停機） ---
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
                        log(f"toggle pause -> {paused['scan']}", "KEY")
                    elif ch == "x":
                        if adapter.has_open():
                            try:
                                sym = adapter.open["symbol"]
                                entry = float(adapter.open["entry"])
                                side = adapter.open["side"]
                                nowp = adapter.best_price(sym)
                                pct = (nowp-entry)/entry if side=="LONG" else (entry-nowp)/entry
                                log_trade(sym, side, adapter.open.get("qty",0), entry, nowp, pct, "hotkey_x")
                                day.on_trade_close(pct)
                                adapter.open = None
                                log("force close position", "KEY")
                            except Exception as e:
                                log(f"close error: {e}", "KEY")
                        else:
                            log("no position to close", "KEY")
                    elif ch == "!":
                        day.state.halted = True
                        log("manual HALT for today", "KEY")
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    threading.Thread(target=_keyloop, daemon=True).start()


    while True:
        day.rollover()

        # 1) 平倉監控
        if adapter.has_open():
            closed, pct, sym = adapter.poll_and_close_if_hit(day)
            if closed:
                log(f"CLOSE {sym} pct={pct*100:.2f}% day={day.state.pnl_pct*100:.2f}%")
                position_view = None
        else:
            # 2) 無持倉：若未停機，掃描與找入場
            if not day.state.halted:
                t = time.time()
                if not paused["scan"] and (t - last_scan > SCAN_INTERVAL_S):
                    try:
                        top10 = fetch_top_gainers(10)
                        last_scan = t
                        log("top10 ok", "SCAN")
                    except Exception as e:
                        log(f"scan error: {e}", "SCAN")

                # 由上而下找第一個符合量價突破
                candidate = None
                for s, pct, last, vol in top10:
                    if volume_breakout_ok(s):
                        candidate = (s, last); break

                if candidate:
                    symbol, entry = candidate
                    side = "LONG"
                    notional = position_size_notional(equity)
                    qty = max(round(notional / entry, 3), 0.001)
                    sl, tp = compute_bracket(entry, side)
                    adapter.place_bracket(symbol, side, qty, entry, sl, tp)
                    position_view = {"symbol":symbol, "side":side, "qty":qty, "entry":entry, "sl":sl, "tp":tp}
                    log(f"OPEN {symbol} qty={qty} entry={entry:.6f}", "ORDER")

        # 3) 輸出給面板
        yield {
            "top10": top10,
            "day_state": day.state,
            "position": adapter.open if hasattr(adapter, "open") else (None if position_view is None else position_view),
            "events": events,
            "account": account,
        }

        time.sleep(0.8)

if __name__ == "__main__":
    try:
        live_render(state_iter())
    finally:
        try:
            stop_ws()
        except Exception:
            pass
