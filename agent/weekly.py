"""Weekly summary: how the bot did over the last 7 days. Saved to reports/weekly/
and sent to Telegram. Read-only: it never trades."""

from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .broker import BrokerError
from .fmt import price as fmt_price
from .ledger import Ledger
from .settings import Settings

log = logging.getLogger(__name__)
MELB = ZoneInfo("Australia/Melbourne")
BULLISH = {"Buy", "Overweight"}
BEARISH = {"Underweight", "Sell"}
BENCHMARKS = (("BTC/USD", True, "Bitcoin"), ("SPY", False, "S&P 500 (SPY)"))


def _t(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:+.1f}%"


def _usd(x: float) -> str:
    return f"-${abs(x):,.2f}" if x < 0 else f"${x:,.2f}"


def build_weekly(settings: Settings, ledger: Ledger, broker, now: datetime | None = None,
                 days: int = 7) -> dict:
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    kinds = {i.ticker: i.kind for i in settings.instruments}
    by_symbol = {i.broker_symbol: i for i in settings.instruments if i.tradable}

    # -- current book at live prices
    holdings = ledger.holdings()
    prices: dict[str, float] = {}
    notes: list[str] = []
    for sym in set(holdings) | set(by_symbol):
        try:
            prices[sym] = broker.latest_price(sym, "/" in sym)
        except BrokerError as exc:
            notes.append(f"no live price for {sym}: {exc}")
    value_now = sum(h["qty"] * prices.get(s, 0.0) for s, h in holdings.items())
    pnl_now = value_now - ledger.data["net_invested"]

    # -- P&L at the start of the week: the last snapshot before `start`, else zero (new bot)
    snaps = [s for s in ledger.data.get("snapshots", []) if _t(s["time"]) <= start]
    pnl_start = snaps[-1]["pnl"] if snaps else 0.0
    runs_this_week = [s for s in ledger.data.get("snapshots", []) if _t(s["time"]) > start]

    trades = [t for t in ledger.data["trades"] if _t(t["time"]) > start]
    realized_week = sum(t.get("realized", 0.0) for t in trades)

    # -- how the week's calls have played out so far (price at call vs now)
    calls = [r for r in ledger.data.get("ratings_log", []) if _t(r["time"]) > start]
    ticker_sym = {i.ticker: i.broker_symbol for i in settings.instruments if i.tradable}
    groups: dict[str, list[float]] = {"bullish": [], "bearish": [], "hold": []}
    for r in calls:
        sym = ticker_sym.get(r["ticker"])
        if not sym or not r.get("price") or sym not in prices:
            continue
        move = prices[sym] / r["price"] - 1
        key = ("bullish" if r["rating"] in BULLISH else
               "bearish" if r["rating"] in BEARISH else
               "hold" if r["rating"] == "Hold" else None)
        if key:
            groups[key].append(move)
    latest: dict[str, str] = {}
    changes: dict[str, list[str]] = {}
    for r in calls:
        if r["rating"] in ("ERROR",):
            continue
        prev = latest.get(r["ticker"])
        if prev and prev != r["rating"]:
            changes.setdefault(r["ticker"], []).append(f"{prev}→{r['rating']}")
        latest[r["ticker"]] = r["rating"]
    errors = sum(1 for r in calls if r["rating"] == "ERROR")

    # -- benchmarks over the same window
    bench = []
    for sym, crypto, label in BENCHMARKS:
        try:
            p0 = broker.price_at(sym, crypto, start.astimezone(timezone.utc)
                                 .strftime("%Y-%m-%dT%H:%M:%SZ"))
            p1 = prices.get(sym) or broker.latest_price(sym, crypto)
            bench.append((label, p1 / p0 - 1))
        except BrokerError as exc:
            notes.append(f"benchmark {label}: {exc}")
            bench.append((label, None))

    # -- OpenAI spend over the window (keys are Melbourne dates)
    first_day = start.astimezone(MELB).date().isoformat()
    spend = sum(v for d, v in ledger.data["spend"].items() if d > first_day)

    return {
        "start": start, "end": now, "value_now": value_now, "pnl_now": pnl_now,
        "pnl_week": pnl_now - pnl_start, "realized_week": realized_week,
        "trades": trades, "holdings": holdings, "prices": prices, "groups": groups,
        "latest": latest, "changes": changes, "errors": errors, "bench": bench,
        "spend": spend, "runs": len(runs_this_week), "kinds": kinds, "notes": notes,
        "halted": ledger.halted_reason, "stopped": settings.stop_file.exists(),
    }


def _avg(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def render_markdown(settings: Settings, w: dict) -> str:
    cap = settings.capital_cap_usd
    end_melb = w["end"].astimezone(MELB)
    start_melb = w["start"].astimezone(MELB)
    L = [f"# Weekly summary: {start_melb:%a %d %b} – {end_melb:%a %d %b %Y} ({settings.mode})", "",
         "## Result", "",
         f"- **This week:** {_usd(w['pnl_week'])} ({w['pnl_week'] / cap * 100:+.2f}% of the "
         f"${cap:,.0f} cap), of which {_usd(w['realized_week'])} realised",
         f"- **Since start:** {_usd(w['pnl_now'])}",
         f"- **Holdings now:** {_usd(w['value_now'])} of ${cap:,.0f} cap",
         "- **Benchmarks, same 7 days:** " +
         ", ".join(f"{label} {_pct(x)}" for label, x in w["bench"]),
         f"- **Runs:** {w['runs']} · **Trades:** {len(w['trades'])} · "
         f"**OpenAI spend:** {_usd(w['spend'])}",
         ]
    if w["halted"]:
        L.append(f"- **Buying halted:** {w['halted']}")
    if w["stopped"]:
        L.append("- **STOP switch is on**")
    L += ["", "## Holdings", ""]
    if w["holdings"]:
        L += ["| Symbol | Qty | Avg cost | Now | Value | P&L |", "|---|---|---|---|---|---|"]
        for sym, h in sorted(w["holdings"].items()):
            px = w["prices"].get(sym)
            avg = h["cost"] / h["qty"] if h["qty"] else 0
            val = h["qty"] * px if px else None
            pl = (px / avg - 1) if px and avg else None
            L.append(f"| {sym} | {h['qty']:.6g} | {fmt_price(avg)} | "
                     f"{fmt_price(px)} | "
                     f"{_usd(val) if val is not None else 'n/a'} | {_pct(pl)} |")
    else:
        L.append("None.")
    L += ["", "## Trades this week", ""]
    if w["trades"]:
        for t in w["trades"]:
            when = _t(t["time"]).astimezone(MELB)
            extra = f", realised {_usd(t['realized'])}" if "realized" in t else ""
            L.append(f"- {when:%a %d %b %H:%M} {t['side'].upper()} {t['qty']:.6g} {t['symbol']} "
                     f"@ {fmt_price(t['price'])} ({_usd(t['notional'])}{extra})")
    else:
        L.append("None.")
    g = w["groups"]
    L += ["", "## How this week's calls have played out", "",
          "Price move from each call to now (tradable tickers only). Bullish calls want this "
          "to be positive, bearish calls negative. One week is a small sample.", "",
          f"- Bullish (Buy/Overweight): {len(g['bullish'])} calls, average move {_pct(_avg(g['bullish']))}",
          f"- Bearish (Underweight/Sell): {len(g['bearish'])} calls, average move {_pct(_avg(g['bearish']))}",
          f"- Hold: {len(g['hold'])} calls, average move {_pct(_avg(g['hold']))}"]
    if w["errors"]:
        L.append(f"- Analyses that failed: {w['errors']}")
    L += ["", "## Latest rating per ticker", "", "| Ticker | Type | Latest | Changes this week |",
          "|---|---|---|---|"]
    label = {"us_stock": "US stock", "crypto": "Crypto", "asx": "ASX (signal)"}
    for t, r in w["latest"].items():
        L.append(f"| {t} | {label.get(w['kinds'].get(t), '')} | {r} | "
                 f"{', '.join(w['changes'].get(t, [])) or '-'} |")
    if w["notes"]:
        L += ["", "## Notes", ""] + [f"- {n}" for n in w["notes"]]
    return "\n".join(L) + "\n"


def render_telegram(settings: Settings, w: dict) -> str:
    e = html.escape
    cap = settings.capital_cap_usd
    g = w["groups"]
    lines = [f"<b>Weekly summary · {settings.mode.upper()} · week to "
             f"{w['end'].astimezone(MELB):%a %d %b}</b>", "",
             f"This week: <b>{e(_usd(w['pnl_week']))}</b> ({w['pnl_week'] / cap * 100:+.2f}% of cap)",
             f"Since start: {e(_usd(w['pnl_now']))} · Holdings {e(_usd(w['value_now']))}",
             "Same week: " + ", ".join(f"{e(lbl)} {_pct(x)}" for lbl, x in w["bench"]),
             f"Runs {w['runs']} · Trades {len(w['trades'])} · OpenAI {e(_usd(w['spend']))}", ""]
    if w["holdings"]:
        lines.append("<b>Holdings</b>")
        for sym, h in sorted(w["holdings"].items()):
            px = w["prices"].get(sym)
            avg = h["cost"] / h["qty"] if h["qty"] else 0
            pl = _pct(px / avg - 1) if px and avg else "n/a"
            val = e(_usd(h["qty"] * px)) if px else "n/a"
            lines.append(f"{e(sym)}: {val} ({pl})")
        lines.append("")
    lines.append("<b>Calls so far</b> (move since call)")
    lines.append(f"Bullish {len(g['bullish'])}: {_pct(_avg(g['bullish']))} · "
                 f"Bearish {len(g['bearish'])}: {_pct(_avg(g['bearish']))}")
    asx = [f"{e(t)} {e(r)}" for t, r in w["latest"].items() if w["kinds"].get(t) == "asx"]
    if asx:
        lines += ["", "<b>ASX signals (Stake)</b>", ", ".join(asx)]
    if w["halted"]:
        lines += ["", f"<b>Buying halted:</b> {e(w['halted'])}"]
    if w["stopped"]:
        lines += ["", "<b>STOP switch is on</b>"]
    lines += ["", "<i>Full weekly report saved in reports/weekly/</i>"]
    return "\n".join(lines)


def run_weekly(settings: Settings, broker=None, telegram=None, send: bool = True,
               now: datetime | None = None) -> tuple[Path, str]:
    from .runner import make_broker, make_telegram

    broker = broker or make_broker(settings)
    ledger = Ledger(settings.state_path)
    w = build_weekly(settings, ledger, broker, now=now)
    folder = settings.reports_dir / "weekly"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"week-to-{w['end'].astimezone(MELB):%Y-%m-%d}.md"
    md = render_markdown(settings, w)
    path.write_text(md, encoding="utf-8")
    if send:
        telegram = telegram if telegram is not None else make_telegram(settings)
        if telegram:
            try:
                telegram.send(render_telegram(settings, w))
            except Exception as exc:
                log.warning("weekly telegram failed: %s", exc)
    return path, md
