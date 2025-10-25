from rich.table import Table
from rich.panel import Panel
from rich.layout import Layout
from rich.live import Live
from rich.console import Console
from rich.text import Text
from utils import ws_best_price

console = Console()

def _fmt_last(symbol: str, last_val):
    p = ws_best_price(symbol)
    v = p if p is not None else last_val
    try:
        return f"{float(v):.6g}"
    except Exception:
        return str(v)

def build_top10_table(top10):
    t = Table(title="Top10 Gainers (24h)", expand=True)
    t.add_column("#", justify="right")
    t.add_column("Symbol")
    t.add_column("Change%", justify="right")
    t.add_column("Last", justify="right")
    t.add_column("Vol", justify="right")
    for i, (s, pct, last, vol) in enumerate(top10, 1):
        t.add_row(str(i), s, f"{pct:.2f}%", _fmt_last(s, last), f"{vol:.0f}")
    return t

def build_status_panel(day_state, account: dict | None = None):
    account = account or {}
    txt = Text()
    txt.append(f"Day PnL: {day_state.pnl_pct*100:.2f}%\n")
    txt.append(f"Trades: {day_state.trades}\n")
    txt.append(f"Halted: {day_state.halted}\n")
    if account.get("testnet"):
        txt.append("[TESTNET]\n", style="magenta")
    eq = account.get("equity")
    if eq is not None:
        txt.append(f"Equity: {float(eq):.2f} USDT\n", style="cyan")
    bal = account.get("balance")
    if bal is not None:
        txt.append(f"Balance: {float(bal):.2f} USDT\n", style="cyan")
    return Panel(txt, title="Status", border_style="green" if not day_state.halted else "red" )

def build_position_panel(position):
    if not position:
        return Panel("No open position", title="Position")
    sym = position["symbol"]; side = position["side"]
    qty = position["qty"]; entry = float(position["entry"])
    tp = float(position["tp"]); sl = float(position["sl"])
    txt = Text()
    txt.append(f"{sym} {side}\n")
    txt.append(f"Qty: {qty}\n")
    txt.append(f"Entry: {entry:.6f}\n")
    txt.append(f"TP: {tp:.6f} | SL: {sl:.6f}\n")
    now_p = ws_best_price(sym) or entry
    try:
        now_p = float(now_p)
        txt.append(f"Now: {now_p:.6f}\n")
        if entry > 0:
            if side == "LONG":
                upnl = (now_p - entry) / entry * 100.0
                dist_tp = (tp - now_p) / entry * 100.0
                dist_sl = (now_p - sl) / entry * 100.0
            else:
                upnl = (entry - now_p) / entry * 100.0
                dist_tp = (now_p - tp) / entry * 100.0
                dist_sl = (sl - now_p) / entry * 100.0
            txt.append(f"Unrealized PnL: {upnl:.2f}%\n")
            txt.append(f"To TP: {dist_tp:.2f}% | To SL: {dist_sl:.2f}%\n")
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
