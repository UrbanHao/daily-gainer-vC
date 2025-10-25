from rich.table import Table
from utils import ws_best_price
from rich.panel import Panel
from rich.layout import Layout
from rich.live import Live
from rich.console import Console
from rich.text import Text

console = Console()

def _fmt_last(symbol: str, row_last):
    p = ws_best_price(symbol)
    val = p if p is not None else row_last
    try:
        return f"{float(val):.6g}"
    except Exception:
        return str(val)

def build_top10_table(top10):
    t = Table(title="Top10 Gainers (24h)", expand=True)
    t.add_column("#", justify="right")
    t.add_column("Symbol")
    t.add_column("Change%", justify="right")
    t.add_column("Last", justify="right")
    t.add_column("Vol", justify="right")
    for i, (s, pct, last, vol) in enumerate(top10, 1):
        # 用 WS 即時價（有就用；沒有才用 24h 回傳的 last）
        t.add_row(str(i), s, f"{pct:.2f}%", _fmt_last(s, last), f"{vol:.0f}")
    return t

def build_status_panel(day_state, account: dict | None = None):
    account = account or {}
    txt = Text()
    txt.append(f"Day PnL: {day_state.pnl_pct*100:.2f}%\n")
    txt.append(f"Trades: {day_state.trades}\n")
    txt.append(f"Halted: {day_state.halted}\n")
    # 顯示 Equity / Balance
    equity = account.get("equity")
    if equity is not None:
        txt.append(f"Equity: {float(equity):.2f} USDT\n", style="cyan")
    bal = account.get("balance")
    if bal is not None:
        txt.append(f"Balance: {float(bal):.2f} USDT\n", style="cyan")
    return Panel(txt, title="Status", border_style="green" if not day_state.halted else "red")

def build_position_panel(position):
    if not position:
        return Panel("No open position", title="Position")
    txt = Text()
    sym = position["symbol"]
    side = position["side"]
    qty = position.get("qty")
    entry = float(position.get("entry", 0) or 0.0)
    tp = float(position.get("tp", 0) or 0.0)
    sl = float(position.get("sl", 0) or 0.0)

    txt.append(f"{sym} {side}\n")
    if qty is not None:
        txt.append(f"Qty: {qty}\n")
    txt.append(f"Entry: {entry:.6f}\n")
    txt.append(f"TP: {tp:.6f} | SL: {sl:.6f}\n")

    now_p = ws_best_price(sym) or entry
    try:
        now_p = float(now_p)
        txt.append(f"Now: {now_p:.6f}\n")
        if entry > 0:
            upnl = (now_p - entry) / entry * 100.0 if side == "LONG" else (entry - now_p) / entry * 100.0
            txt.append(f"Unrealized PnL: {upnl:.2f}%\n")
    except Exception:
        pass

    return Panel(txt, title="Position", border_style="yellow")

def build_events_panel(events):
    t = Table(expand=True)
    t.add_column("Time")
    t.add_column("Event")
    for ts, msg in events[-12:]:
        t.add_row(ts, msg)
    return Panel(t, title="Events")

def render_layout(top10, day_state, position, events, account=None):
    layout = Layout()
    layout.split_column(
        Layout(name="upper", ratio=2),
        Layout(name="lower", ratio=1)
    )
    layout["upper"].split_row(
        Layout(build_top10_table(top10), name="top10"),
        Layout(build_status_panel(day_state, account), name="status"),
        Layout(build_position_panel(position), name="pos"),
    )
    layout["lower"].update(build_events_panel(events))
    return layout

def live_render(loop_iterable):
    with Live(refresh_per_second=8, console=console) as live:
        for state in loop_iterable:
            live.update(render_layout(
                state.get("top10", []),
                state["day_state"],
                state.get("position"),
                state.get("events", []),
                state.get("account", {})
            ))
