# ================= 基本參數 =================
DAILY_TARGET_PCT = 0.015     # 當日達標 +1.5%
DAILY_LOSS_CAP   = -0.02     # 當日最大虧損 -2%
PER_TRADE_RISK   = 0.0075    # 每筆風險 0.75% 權益
TP_PCT           = 0.015     # 單筆停利 +1.5%
SL_PCT           = 0.0075    # 單筆止損 -0.75%
MAX_TRADES_DAY   = 6         # 每日最多交易筆數
SCAN_INTERVAL_S  = 25        # Top10 刷新頻率（秒）
USE_LIVE         = False     # 先跑模擬；接實盤請改 True

# ================= 訊號參數（版本 C） =================
KLINE_INTERVAL   = "5m"      # 以 5 分鐘作為訊號級別
KLINE_LIMIT      = 120       # 拉 120 根 5m（≈ 10 小時）
HH_N             = 96        # 過去 N 根的前高（≈ 8 小時）
OVEREXTEND_CAP   = 0.02      # 突破幅度不得超過 +2%
VOL_BASE_WIN     = 48        # 基準量能視窗（近 48 根）
VOL_SPIKE_K      = 2.0       # 量能放大倍率
VOL_LOOKBACK_CONFIRM = 3     # 突破確認期間：近 3 根量能總和
EMA_FAST         = 20        # 結構過濾：EMA20
EMA_SLOW         = 50        # 結構過濾：EMA50

# API 基礎
BINANCE_FUTURES_BASE = "https://fapi.binance.com"  # USDT 永續
# ===== 實盤連線與風控補充 =====
# 先用 Futures 測試網驗證，OK 再改成 False
USE_TESTNET = True

# 限價單等待成交的逾時秒數（逾時會自動撤單）
ORDER_TIMEOUT_SEC = 90
