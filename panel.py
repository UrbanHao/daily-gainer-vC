from rich.table import Table
from rich.panel import Panel
from rich.layout import Layout
from rich.live import Live
from rich.console import Console
from rich.text import Text

console = Console()

def build_top10_table(top10):
    t = Table(title="Top10 Gainers (24h)", expand=True)
    t.add_column("#", justify="right")
    t.add_column("Symbol")
    t.add_column("Change%", justify="right")
    t.add_column("Last", justify="right")
    t.add_column("Vol", justify="right")
    for i, (s, pct, last, vol) in enumerate(top10, 1):
        t.add_row(str(i), s, f"{pct:.2f}%", f"{last:.6f}", f"{vol:.0f}")
    return t

def build_status_panel(day_state, account: dict | None = None):
    account = account or {}
    txt = Text()
    txt.append(f"Day PnL: {day_state.pnl_pct*100:.2f}%\n")
    txt.append(f"Trades: {day_state.trades}\n")
    txt.append(f"Halted: {day_state.halted}\n")
    if account.get("testnet"):
        txt.append("[TESTNET]\n", style="magenta")
    return Panel(txt, title="Status", border_style="green" if not day_state.halted else "red" )

def build_position_panel(position):
    if not position:
        return Panel("No open position", title="Position")
    txt = Text()
    txt.append(f"{position['symbol']} {position['side']}\n")
    txt.append(f"Qty: {position['qty']}\n")
    txt.append(f"Entry: {position['entry']:.6f}\n")
    txt.append(f"TP: {position['tp']:.6f} | SL: {position['sl']:.6f}\n")
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
            # state: dict(top10, day_state, position, events)
            live.update(render_layout(
                state.get("top10", []),
                state["day_state"],
                state.get("position"),
                state.get("events", []),
                state.get("account", {})
            ))