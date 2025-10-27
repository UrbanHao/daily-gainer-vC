from dotenv import load_dotenv
import os
load_dotenv(override=True)

# ================= 基本參數 =================
SYMBOL_BLACKLIST = [
    "ALPACAUSDT",
    "BNXUSDT",
    # 在這裡加入其他你想拉黑的幣種 (記得加引號和逗號)
]

DAILY_TARGET_PCT = float(os.getenv("DAILY_TARGET_PCT", "0.15"))     # 當日達標 +15%
DAILY_LOSS_CAP   = float(os.getenv("DAILY_LOSS_CAP", "-0.05"))      # 當日最大虧損 -5%
PER_TRADE_RISK   = float(os.getenv("PER_TRADE_RISK", "0.0075"))     # 每筆風險 0.75% 權益
TP_PCT           = float(os.getenv("TP_PCT", "0.015"))              # 單筆停利 +1.5%
SL_PCT           = float(os.getenv("SL_PCT", "0.0075"))             # 單筆止損 -0.75%
MAX_TRADES_DAY   = int(os.getenv("MAX_TRADES_DAY", "10"))           # 每日最多交易筆數
SCAN_INTERVAL_S  = int(os.getenv("SCAN_INTERVAL_S", "10"))          # 掃描刷新頻率（秒）
SCAN_TOP_N       = int(os.getenv("SCAN_TOP_N", "30"))               # 掃描 Top N 幣種
ALLOW_SHORT      = os.getenv("ALLOW_SHORT", "True").lower() == "true" # <-- 新增：是否允許做空
USE_LIVE         = os.getenv("USE_LIVE", "True").lower() == "true"    # 實盤開關

# --- Large Trades (time-base, from WS aggTrade) ---
LARGE_TRADES_ENABLED = os.getenv("LARGE_TRADES_ENABLED", "True").lower() == "true"
LARGE_TRADES_MERGE_S = int(os.getenv("LARGE_TRADES_MERGE_S", "5"))
LARGE_TRADES_FILTER_MODE = os.getenv("LARGE_TRADES_FILTER_MODE", "Percentile")   # "Percentile" | "Absolute"
LARGE_TRADES_BUY_PCT  = float(os.getenv("LARGE_TRADES_BUY_PCT",  "90"))
LARGE_TRADES_SELL_PCT = float(os.getenv("LARGE_TRADES_SELL_PCT", "90"))
LARGE_TRADES_BUY_ABS  = float(os.getenv("LARGE_TRADES_BUY_ABS",  "1000000"))
LARGE_TRADES_SELL_ABS = float(os.getenv("LARGE_TRADES_SELL_ABS", "1000000"))
LARGE_TRADES_ANCHOR_DRIFT = float(os.getenv("LARGE_TRADES_ANCHOR_DRIFT", "0.001"))  # 0.1%

# ================= 訊號參數（版本 C） =================
KLINE_INTERVAL   = os.getenv("KLINE_INTERVAL", "5m")      # 以 5 分鐘作為訊號級別
KLINE_LIMIT      = int(os.getenv("KLINE_LIMIT", "120"))       # 拉 120 根 5m（≈ 10 小時）
HH_N             = int(os.getenv("HH_N", "96"))        # 過去 N 根的前高（≈ 8 小時）
OVEREXTEND_CAP   = float(os.getenv("OVEREXTEND_CAP", "0.02"))      # 突破幅度不得超過 +2%
VOL_BASE_WIN     = int(os.getenv("VOL_BASE_WIN", "48"))        # 基準量能視窗（近 48 根）
VOL_SPIKE_K      = float(os.getenv("VOL_SPIKE_K", "2.0"))       # 量能放大倍率
VOL_LOOKBACK_CONFIRM = int(os.getenv("VOL_LOOKBACK_CONFIRM", "3"))     # 突破確認期間：近 3 根量能總和
EMA_FAST         = int(os.getenv("EMA_FAST", "20"))        # 結構過濾：EMA20
EMA_SLOW         = int(os.getenv("EMA_SLOW", "50"))        # 結構過濾：EMA50

# API 基礎
BINANCE_FUTURES_BASE = "https://fapi.binance.com"  # USDT 永續
BINANCE_FUTURES_TEST_BASE = "https://testnet.binancefuture.com" # 測試網

# ===== 實盤連線與風控補充 =====
# 先用 Futures 測試網驗證，OK 再改成 False
USE_TESTNET = os.getenv("USE_TESTNET", "False").lower() == "true"

# 限價單等待成交的逾時秒數（逾時會自動撤單）
ORDER_TIMEOUT_SEC = int(os.getenv("ORDER_TIMEOUT_SEC", "90"))

# WebSocket 開關：面板即時價
USE_WEBSOCKET = os.getenv("USE_WEBSOCKET", "True").lower() == "true"
