# -*- coding: utf-8 -*-
"""
穩定的 Binance USDT-M Futures WebSocket 客戶端：
- start_ws(symbols, use_testnet): 啟動/更新訂閱清單（自動重連）
- stop_ws(): 結束背景執行緒
- ws_best_price(symbol): 讀取最新價（若還沒有，就回 None）

注意：
- symbols 必須是 Futures 的交易對（例如 "BTCUSDT", "ETHUSDT"...），無斜線。
- 這裡使用 24hr ticker stream：<symbol>@ticker
"""

import threading, json, time, traceback
from collections import defaultdict
from websockets import connect

_prices = defaultdict(lambda: None)
_lock = threading.Lock()
_stop = threading.Event()
_runner = None
_subscribed = set()
_host_main = "wss://fstream.binance.com/stream?streams="
_host_test = "wss://stream.binancefuture.com/stream?streams="  # USDT-M 測試網
_current_url = None

def ws_best_price(symbol: str):
    """回傳最新價（float）或 None"""
    with _lock:
        v = _prices.get(symbol)
    return float(v) if (v is not None and v != "") else None

def _mk_url(symbols, use_testnet: bool):
    host = _host_test if use_testnet else _host_main
    # Futures 24h ticker stream： <symbol>@ticker
    streams = "/".join([f"{s.lower()}@ticker" for s in symbols])
    return host + streams

async def _run(url: str):
    global _current_url
    _current_url = url
    while not _stop.is_set():
        try:
            async with connect(url, ping_interval=20, ping_timeout=20) as ws:
                # 讀資料
                while not _stop.is_set():
                    msg = await ws.recv()
                    data = json.loads(msg)
                    # binance multiplex 有 'data' 欄位；單一流可能直接是字典
                    d = data.get("data", data)
                    # 24hr ticker 有 's' (symbol), 'c' (lastPrice)
                    s = d.get("s")
                    c = d.get("c")
                    if s and c is not None:
                        with _lock:
                            _prices[s] = c
        except Exception:
            # 記錄再重試
            traceback.print_exc()
            time.sleep(2)

def start_ws(symbols, use_testnet: bool):
    """啟動或更新訂閱；若 symbols 與現有不同，會重啟連線。"""
    global _runner, _subscribed
    new = set(symbols or [])
    if not new:
        return
    url = _mk_url(sorted(new), use_testnet)

    # 若相同訂閱，直接返回
    if new == _subscribed and _current_url == url and _runner and _runner.is_alive():
        return

    # 更新訂閱：先停舊，再啟新
    stop_ws()

    _stop.clear()
    _subscribed = new
    _runner = threading.Thread(target=_thread_main, args=(url,), daemon=True)
    _runner.start()

def _thread_main(url):
    import asyncio
    try:
        asyncio.run(_run(url))
    except RuntimeError:
        # 有些環境 event loop 存在，fallback
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_run(url))

def stop_ws():
    """停止連線與背景執行緒"""
    global _runner
    _stop.set()
    if _runner and _runner.is_alive():
        # 等一等讓連線關閉
        for _ in range(20):
            if not _runner.is_alive():
                break
            time.sleep(0.1)
    _runner = None
