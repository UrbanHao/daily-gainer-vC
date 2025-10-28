import json, threading, time, asyncio
from typing import Dict, List, Optional
from collections import deque, defaultdict
import websockets

_WS_THREAD = None
_WS_STOP = False
_SUBS: List[str] = [] # Track current subscriptions
_HOST = {"test": "wss://stream.binancefuture.com", "main": "wss://fstream.binance.com"}
_RECEIVED_TICKER_SYMBOLS = set() # <--- 新增這一行

# --- 全域快取 ---
# 1. 價格快取
_PRICE: Dict[str, float] = {}

# 2. 逐筆成交快取: { "BTCUSDT": deque([(ts_ms, price, qty, is_buy), ...]) }
_AGG: Dict[str, deque] = defaultdict(lambda: deque(maxlen=6000))

# --- 讀取快取的函數 ---

def ws_best_price(symbol: str) -> Optional[float]:
    """讀取最新價格"""
    return _PRICE.get(symbol.upper())

def ws_recent_agg(symbol: str, window_s: int = 30) -> List: # <--- 確認這行存在！
    """讀取近 window_s 秒的逐筆成交"""
    cutoff = int(time.time() * 1000) - window_s * 1000
    dq = _AGG.get(symbol.upper())
    if not dq: return []
    results = []
    # Iterate from right (newest) to left (oldest) for efficiency
    for item in reversed(dq):
        if item[0] >= cutoff:
            results.append(item)
        else:
            break # Deque is time-ordered
    # Results are newest-to-oldest, signal side doesn't care about order within window
    return results

# --- WS 訊息處理 ---

def _on_ticker(msg: dict):
    """處理 @ticker 訊息"""
    s = msg.get("s")
    c = msg.get("c")
    if s and c:
        # --- 加入 Debug Print ---
        # 只有第一次收到某幣種的 ticker 時才打印，避免洗版
        if s not in _RECEIVED_TICKER_SYMBOLS:
            print(f"DEBUG WS: Received first ticker update for {s}")
            _RECEIVED_TICKER_SYMBOLS.add(s)
        # --- 結束 Debug Print ---
        try:
            _PRICE[s] = float(c)
        except ValueError:
            pass # Ignore conversion errors

def _on_aggtrade(msg: dict):
    """處理 @aggTrade 訊息"""
    s = msg.get("s")
    if not s: return
    try:
        ts = int(msg.get("T", 0))
        p  = float(msg.get("p", 0) or 0)
        q  = float(msg.get("q", 0) or 0)
        is_buy = not bool(msg.get("m", False)) # Taker Buy
        if p > 0 and q > 0 and ts > 0:
            _AGG[s].append((ts, p, q, is_buy))
    except (ValueError, KeyError, TypeError):
        pass # Ignore parsing errors

async def _run_ws(loop_syms: List[str], use_testnet: bool):
    global _PRICE, _AGG
    url_base = (_HOST["test"] if use_testnet else _HOST["main"])

    streams = []
    for s in loop_syms:
        s_low = s.lower()
        streams.append(f"{s_low}@ticker")
        streams.append(f"{s_low}@aggTrade")

    url = f"{url_base}/stream?streams={'/'.join(streams)}"

    while not _WS_STOP:
        try:
            async with websockets.connect(url, ping_interval=15, ping_timeout=15) as ws:
                while not _WS_STOP:
                    msg_raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    d = json.loads(msg_raw)

                    data = d.get("data")
                    stream_name = d.get("stream") # Get stream name to identify type
                    if not data or not stream_name:
                        continue

                    # Determine message type based on stream name or event type
                    if "@ticker" in stream_name:
                         _on_ticker(data)
                    elif "@aggTrade" in stream_name:
                         _on_aggtrade(data)
                    # Fallback check using event type if stream name wasn't clear
                    elif data.get("e") == "ticker":
                         _on_ticker(data)
                    elif data.get("e") == "aggTrade":
                         _on_aggtrade(data)

        except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed):
            print("WebSocket timeout or closed, reconnecting...")
            await asyncio.sleep(1.0) # Wait before reconnecting
        except Exception as e:
            print(f"WebSocket error: {e}, reconnecting...")
            await asyncio.sleep(1.0) # Wait before reconnecting

def start_ws(symbols: List[str], use_testnet: bool):
    """啟動/更新訂閱；重複呼叫會更新 _SUBS 並重啟 thread。"""
    global _WS_THREAD, _WS_STOP, _SUBS
    syms = [s.upper() for s in symbols]
    # Only restart if symbols actually changed or thread died
    if syms == _SUBS and _WS_THREAD and _WS_THREAD.is_alive():
        return
    _SUBS = syms
    stop_ws() # Ensure previous thread is stopped
    _WS_STOP = False
    def _t():
        # Set up a new event loop for the thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_run_ws(_SUBS, use_testnet))
        finally:
            loop.close()

    _WS_THREAD = threading.Thread(target=_t, daemon=True)
    _WS_THREAD.start()
    print(f"WebSocket started/restarted for {len(syms)} symbols.")


def stop_ws():
    """停止 WebSocket 執行緒並清除快取"""
    global _WS_THREAD, _WS_STOP
    _WS_STOP = True
    if _WS_THREAD and _WS_THREAD.is_alive():
        try:
            _WS_THREAD.join(timeout=1.0) # Give more time to join
        except Exception as e:
            print(f"Error stopping WebSocket thread: {e}")
    _WS_THREAD = None
    _PRICE.clear() # Clear cache on stop
    _AGG.clear()
    _SUBS = [] # Clear subscriptions list
    _RECEIVED_TICKER_SYMBOLS.clear() # <--- 新增這一行
    print("WebSocket stopped.")
