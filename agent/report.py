"""Daily markdown report on disk, and a short Telegram summary."""

from __future__ import annotations

import html
import logging
from pathlib import Path

log = logging.getLogger(__name__)


def write_report(settings, result, dry_run: bool = False) -> Path:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    melb = datetime.now(ZoneInfo("Australia/Melbourne"))
    suffix = "-dry-run" if dry_run else ""
    if getattr(result, "scope", "all") == "crypto":
        # crypto checks run every few hours: one file per check, in their own folder
        folder = settings.reports_dir / "crypto"
        name = f"{melb:%Y-%m-%d-%H%M}{suffix}.md"
        title = f"crypto check {melb:%a %d %b %H:%M} Melbourne"
    else:
        folder = settings.reports_dir
        name = f"{result.trade_date}{suffix}.md"
        title = f"{result.trade_date} US session"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    if path.exists():  # a second run for the same session keeps the first report
        path = folder / f"{path.stem}-{melb:%H%M%S}.md"
    kinds = {i.ticker: i.kind for i in settings.instruments}
    L = [f"# Trading agent report: {title} ({result.mode}{', dry run' if dry_run else ''})",
         "",
         f"- Bot holdings value: ${result.holdings_value:,.2f} of ${settings.capital_cap_usd:,.0f} cap",
         f"- Bot P&L since start: ${result.pnl:,.2f}",
         f"- OpenAI spend today: ${result.spend_today:,.2f} (budget ${settings.daily_budget_usd:,.2f})",
         "", "## Ratings", "", "| Ticker | Type | Rating | Why (short) |", "|---|---|---|---|"]
    for a in result.analyses:
        kind = {"us_stock": "US stock", "crypto": "Crypto", "asx": "ASX (signal)"}.get(
            kinds.get(a.ticker, ""), "")
        L.append(f"| {a.ticker} | {kind} | **{a.rating}** | {a.summary.replace('|', '/')} |")
    plan = result.plan
    L += ["", "## Orders", ""]
    if result.submitted:
        L += ["Placed:"] + [f"- {s}" for s in result.submitted]
    if result.declined:
        L += ["Not approved:"] + [f"- {s}" for s in result.declined]
    if result.failed:
        L += ["Failed:"] + [f"- {s}" for s in result.failed]
    if plan and plan.orders and dry_run:
        L += ["Would place (dry run):"] + [f"- {o.describe()}" for o in plan.orders]
    if plan and plan.skipped:
        L += ["Skipped:"] + [f"- {s}" for s in plan.skipped]
    if not (result.submitted or result.declined or result.failed or (plan and plan.orders)):
        L.append("No trades today.")
    if result.notes:
        L += ["", "## Notes", ""] + [f"- {n}" for n in result.notes]
    L += ["", "## Full decisions", ""]
    for a in result.analyses:
        L += [f"### {a.ticker}: {a.rating}", "", a.full_decision or a.error or "(none)", ""]
    L += ["---", "Research tool output, not financial advice."]
    path.write_text("\n".join(L), encoding="utf-8")
    return path


def send_summary(settings, telegram, result, dry_run: bool = False) -> None:
    if not telegram:
        return
    e = html.escape
    kinds = {i.ticker: i.kind for i in settings.instruments}
    label = "crypto check" if getattr(result, "scope", "all") == "crypto" else result.trade_date
    lines = [f"<b>Trading agent · {result.mode.upper()}{' · DRY RUN' if dry_run else ''} · "
             f"{label}</b>", ""]
    groups = (("us_stock", "US stocks"), ("crypto", "Crypto"), ("asx", "ASX signals (trade on Stake)"))
    for kind, title in groups:
        rows = [a for a in result.analyses if kinds.get(a.ticker) == kind]
        if rows:
            lines.append(f"<b>{title}</b>")
            lines += [f"{e(a.ticker)}: <b>{e(a.rating)}</b>" for a in rows]
            lines.append("")
    if result.submitted:
        lines += ["<b>Orders placed</b>"] + [e(s) for s in result.submitted] + [""]
    if result.declined:
        lines += ["<b>Not placed</b>"] + [e(s) for s in result.declined] + [""]
    if result.failed:
        lines += ["<b>Failed</b>"] + [e(s) for s in result.failed] + [""]
    if dry_run and result.plan and result.plan.orders:
        lines += ["<b>Would place</b>"] + [e(o.describe()) for o in result.plan.orders] + [""]
    lines.append(f"Holdings ${result.holdings_value:,.2f} / ${settings.capital_cap_usd:,.0f} · "
                 f"P&amp;L ${result.pnl:,.2f} · OpenAI ${result.spend_today:,.2f}")
    if result.notes:
        lines += ["", "<i>" + e("; ".join(result.notes))[:800] + "</i>"]
    try:
        telegram.send("\n".join(lines))
    except Exception as exc:  # the run already happened; do not fail it over a message
        log.warning("telegram summary failed: %s", exc)
