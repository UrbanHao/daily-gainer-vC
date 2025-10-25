import asyncio, json, threading, time
from typing import Dict, List, Optional
import websockets

# 價格快取（symbol -> (price, ts)）
_PRICE: Dict[str, tuple] = {}
_LOCK = threading.Lock()

# 目前訂閱的 symbols（小寫）
_current_stream: Optional[str] = None
_stop = False
_thread: Optional[threading.Thread] = None

def _build_url(symbols: List[str], use_testnet: bool) -> str:
    # futures USDT-M:
    # 正式網：wss://fstream.binance.com/stream?streams=btcusdt@miniTicker
    # 測試網：wss://stream.binancefuture.com/stream?streams=btcusdt@miniTicker
    base = "wss://stream.binancefuture.com" if use_testnet else "wss://fstream.binance.com"
    streams = "/".join([f"{s.lower()}@miniTicker" for s in symbols])
    return f"{base}/stream?streams={streams}"

async def _run(symbols: List[str], use_testnet: bool):
    global _current_stream
    url = _build_url(symbols, use_testnet)
    _current_stream = url
    backoff = 1
    while not _stop:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_queue=1024) as ws:
                backoff = 1
                while not _stop:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    # combined stream payload: {"stream":"xxx","data":{...}}
                    d = data.get("data", {})
                    sym = d.get("s")
                    c = d.get("c")
                    if sym and c:
                        try:
                            price = float(c)
                            with _LOCK:
                                _PRICE[sym.upper()] = (price, time.time())
                        except Exception:
                            pass
        except Exception:
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 20)

def start_ws(symbols: List[str], use_testnet: bool):
    """啟動/重啟 WebSocket 訂閱（依 symbols 清單更新）"""
    global _thread, _stop
    stop_ws()
    _stop = False
    # 啟用背景執行緒跑 asyncio event loop
    def _bg():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_run(symbols, use_testnet))
    _thread = threading.Thread(target=_bg, daemon=True)
    _thread.start()

def stop_ws():
    """停止當前 WebSocket"""
    global _stop, _thread
    _stop = True
    if _thread and _thread.is_alive():
        # 給背景 loop 一點時間退出
        time.sleep(0.2)
    _thread = None

def get_price(symbol: str, fresh_sec: float = 10.0) -> Optional[float]:
    """取得最新價（若快取太舊則回 None）"""
    symbol = symbol.upper()
    with _LOCK:
        tup = _PRICE.get(symbol)
    if not tup: return None
    p, ts = tup
    if time.time() - ts <= fresh_sec:
        return p
    return None
