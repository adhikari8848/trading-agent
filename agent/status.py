"""Print the bot's holdings, P&L, open orders and recent OpenAI spend."""

from __future__ import annotations

from .fmt import price as fmt_price
from .ledger import Ledger
from .settings import Settings


def print_status(s: Settings) -> int:
    led = Ledger(s.state_path)
    print(f"Mode: {s.mode.upper()}   Cap: ${s.capital_cap_usd:,.0f}")
    if s.stop_file.exists():
        print("STOP switch: ON (no orders will be placed)")
    if led.halted_reason:
        print(f"Buying halted: {led.halted_reason}")
    _print_schedule(s, led)

    prices: dict[str, float] = {}
    try:
        from .runner import make_broker
        b = make_broker(s)
        acct = b.account()
        pos = b.positions()
        prices = {k: p.current_price for k, p in pos.items()}
        print(f"Alpaca account: cash ${acct.cash:,.2f}, equity ${acct.equity:,.2f}")
    except Exception as exc:
        print(f"(could not reach Alpaca: {exc})")

    hold = led.holdings()
    print("\nBot holdings:" if hold else "\nBot holdings: none")
    value = 0.0
    for sym, h in sorted(hold.items()):
        px = prices.get(sym)
        mv = h["qty"] * px if px else None
        value += mv or 0.0
        avg = h["cost"] / h["qty"] if h["qty"] else 0
        mv_s = f"${mv:,.2f}" if mv is not None else "n/a"
        print(f"  {sym:10s} qty {h['qty']:.6g}  avg {fmt_price(avg)}  value {mv_s}")
    if prices:
        print(f"Total value ${value:,.2f}  |  P&L ${value - led.data['net_invested']:,.2f}"
              f"  (realised ${led.data['realized_pnl']:,.2f})")

    pend = led.data["pending_orders"]
    if pend:
        print("\nOpen orders:")
        for oid, p in pend.items():
            print(f"  {p['side']} {p['qty']:.6g} {p['symbol']} limit {fmt_price(p['limit'])} ({oid[:8]})")

    spend = sorted(led.data["spend"].items())[-7:]
    if spend:
        print("\nOpenAI spend (last 7 run days): " +
              ", ".join(f"{d} ${v:.2f}" for d, v in spend) +
              f"  | total ${sum(led.data['spend'].values()):.2f}")
    trades = led.data["trades"][-5:]
    if trades:
        print("\nLast trades:")
        for t in trades:
            print(f"  {t['time'][:16]} {t['side']} {t['qty']:.6g} {t['symbol']} @ {fmt_price(t['price'])}")
    return 0


def _print_schedule(s: Settings, led: Ledger) -> None:
    import os
    from zoneinfo import ZoneInfo

    from .runner import MOVED_MARKER
    from .schedule import PREFIX, describe, installed

    melb = ZoneInfo("Australia/Melbourne")
    if os.getenv("GITHUB_ACTIONS"):
        print("Running on GitHub Actions (schedules: .github/workflows/)")
        _print_last_runs(led, melb)
        return
    if (s.project_dir / MOVED_MARKER).exists():
        print("NOTE: the bot runs on GitHub Actions now. This Mac copy of the book is out of date;\n"
              "      see the repo's 'state' branch or run 'status' from the Actions tab.")
    inst = installed()
    on = [k for k in (f"{PREFIX}.stocks", f"{PREFIX}.crypto", f"{PREFIX}.weekly") if inst.get(k)]
    if on:
        print(f"Schedules: {len(on)}/3 installed")
        for line in describe(s):
            print(f"  - {line}")
    elif inst.get(PREFIX):
        print("Schedules: old single daily job installed. Run ./ta install-schedule to upgrade.")
    else:
        print("Schedules: none installed (./ta install-schedule)")
    _print_last_runs(led, melb)


def _print_last_runs(led: Ledger, melb) -> None:
    from datetime import datetime
    for scope in ("stocks", "crypto"):
        last = led.last_run(scope)
        when = datetime.fromisoformat(last).astimezone(melb).strftime("%a %d %b %H:%M") if last else "never"
        print(f"  Last {scope} run (Melbourne): {when}")
