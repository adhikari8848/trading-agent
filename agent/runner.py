"""One run: sync the account, analyse, plan, approve, trade, report.

Scopes
- stocks: US stocks (traded) and ASX (signals). Once per US trading day.
- crypto: BTC/ETH/SOL. Every few hours, every day of the week.
- all:    both (a manual run).
"""

from __future__ import annotations

import fcntl
import html
import logging
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .analyst import Analysis, Analyst
from .broker import AlpacaBroker, AssetInfo, BrokerError
from .costs import BudgetExceeded, SpendTracker
from .fmt import price as fmt_price
from .ledger import Ledger
from .planner import Plan, build_plan
from .settings import Instrument, Settings
from .telegram import Telegram, TelegramError

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")
MELB = ZoneInfo("Australia/Melbourne")
SCOPES = {"stocks": {"us_stock", "asx"}, "crypto": {"crypto"},
          "all": {"us_stock", "asx", "crypto"}}
# A crypto run is due once this much of the interval has passed since the last one,
# so a run that started late (Mac just woke) doesn't block the next scheduled slot.
CRYPTO_DUE_FRACTION = 0.8


class RunError(RuntimeError):
    pass


def session_date(now: datetime) -> str:
    """The US session the run is about: today's date in New York once the 4pm
    close has passed, otherwise yesterday's. The 8:30am Melbourne schedule is
    always after the close; a manual run in the Melbourne afternoon (New York
    early morning) belongs to the previous session, not one that hasn't traded."""
    et = now.astimezone(NY)
    day = et.date() if et.hour >= 16 else et.date() - timedelta(days=1)
    return day.isoformat()


def crypto_date(now: datetime) -> str:
    """Crypto trades around the clock; its daily candles are cut at midnight UTC."""
    return now.astimezone(timezone.utc).date().isoformat()


@dataclass
class RunResult:
    trade_date: str
    mode: str
    scope: str = "all"
    analyses: list[Analysis] = field(default_factory=list)
    plan: Plan | None = None
    submitted: list[str] = field(default_factory=list)
    declined: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    spend_today: float = 0.0
    pnl: float = 0.0
    holdings_value: float = 0.0
    report_path: str | None = None
    ran: bool = False          # False when nothing was due
    halted_now: bool = False


@contextmanager
def run_lock(settings: Settings, wait_minutes: float = 0, sleep=time.sleep):
    """One run at a time. Scheduled runs wait their turn instead of being dropped."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    fh = open(settings.data_dir / ".run.lock", "w")
    deadline = time.monotonic() + wait_minutes * 60
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError as exc:
            if time.monotonic() >= deadline:
                fh.close()
                raise RunError("another run is already in progress") from exc
            sleep(15)
    try:
        yield
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def make_broker(settings: Settings) -> AlpacaBroker:
    return AlpacaBroker(settings.alpaca_key_id or "", settings.alpaca_secret or "",
                        live=settings.is_live)


def make_telegram(settings: Settings) -> Telegram | None:
    if settings.telegram_enabled:
        return Telegram(settings.telegram_token, settings.telegram_chat_id)
    return None


MOVED_MARKER = ".runs-on-github"


def moved_to_github(settings: Settings) -> str | None:
    """After the move to GitHub Actions, the Mac copy must not trade too: two bots
    with separate ledgers on one Alpaca account would double every position."""
    if os.getenv("GITHUB_ACTIONS") or os.getenv("TA_ALLOW_LOCAL"):
        return None
    if (settings.project_dir / MOVED_MARKER).exists():
        return ("this bot now runs on GitHub Actions, so this Mac copy won't trade. "
                "Use the Actions tab > 'manual run' instead (or set TA_ALLOW_LOCAL=1 to override).")
    return None


def _stocks_done(ledger: Ledger, session: str) -> bool:
    # "YYYY-MM-DD" is the key older versions used for the (then single) daily run
    return ledger.run_status(f"stocks:{session}") == "done" or ledger.run_status(session) == "done"


def _crypto_due(settings: Settings, ledger: Ledger, now: datetime) -> bool:
    last = ledger.last_run("crypto")
    if not last:
        return True
    elapsed = now - datetime.fromisoformat(last)
    return elapsed >= timedelta(hours=settings.crypto_every_hours * CRYPTO_DUE_FRACTION)


def run(settings: Settings, *, scope: str = "all", dry_run: bool = False,
        only: list[str] | None = None, force: bool = False, wait_minutes: float = 0,
        broker: AlpacaBroker | None = None, telegram: Telegram | None = None,
        analyst: Analyst | None = None, now: datetime | None = None,
        sleep=time.sleep) -> RunResult:
    if scope not in SCOPES:
        raise RunError(f"unknown scope {scope!r}; use stocks, crypto or all")
    now = now or datetime.now(tz=NY)
    session = session_date(now)
    budget_day = now.astimezone(MELB).date().isoformat()
    result = RunResult(trade_date=session, mode=settings.mode, scope=scope)

    moved = moved_to_github(settings)
    if moved:
        raise RunError(moved)
    if settings.is_live and not settings.telegram_enabled:
        raise RunError("live mode needs Telegram set up (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)")
    if not settings.openai_api_key and settings.llm_provider == "openai":
        raise RunError("OPENAI_API_KEY is not set in .env")

    with run_lock(settings, wait_minutes, sleep=sleep):
        ledger = Ledger(settings.state_path)

        # 0. What is due? (a manual --force or --only run always goes ahead)
        kinds = set(SCOPES[scope])
        if not force and not only:
            if "us_stock" in kinds and _stocks_done(ledger, session):
                kinds -= {"us_stock", "asx"}
            if "crypto" in kinds and not _crypto_due(settings, ledger, now):
                kinds -= {"crypto"}
            if not kinds:
                result.notes.append("nothing due: this run already happened")
                log.info("nothing due for scope %s", scope)
                return result
        result.ran = True

        broker = broker or make_broker(settings)
        telegram = telegram if telegram is not None else make_telegram(settings)

        _handle_commands(settings, ledger, telegram, result)

        acct = broker.account()
        if acct.status != "ACTIVE" or acct.trading_blocked:
            raise RunError(f"Alpaca account not tradable (status {acct.status})")

        # 1. Book fills from earlier orders, then line the ledger up with the account.
        _sync_orders(broker, ledger, result)
        positions = broker.positions()
        result.notes += ledger.reconcile({s: p.qty for s, p in positions.items()})
        bot_qty = {s: min(ledger.qty(s), positions[s].qty if s in positions else 0.0)
                   for s in ledger.holdings()}

        tradable = [i for i in settings.instruments if i.tradable]
        crypto_syms = {i.broker_symbol for i in tradable if i.kind == "crypto"}
        prices: dict[str, float] = {s: p.current_price for s, p in positions.items()}
        for inst in tradable:
            try:
                prices[inst.broker_symbol] = broker.latest_price(
                    inst.broker_symbol, inst.broker_symbol in crypto_syms)
            except BrokerError as exc:
                result.notes.append(f"price for {inst.broker_symbol}: {exc}")

        market_value = {s: q * prices.get(s, 0.0) for s, q in bot_qty.items()}
        result.holdings_value = sum(market_value.values())
        result.pnl = ledger.pnl(market_value)
        if result.pnl <= -settings.max_loss_usd and not ledger.halted_reason:
            ledger.halt(f"P&L ${result.pnl:,.2f} passed the -${settings.max_loss_usd:,.0f} limit")
            result.halted_now = True
            _notify(telegram, f"<b>Buying halted.</b> {html.escape(ledger.halted_reason)}. "
                              "Send /resume when you have reviewed it.")

        # 2. Decide what to analyse.
        todays = _instruments_for_today(settings, broker, session, only, bot_qty, result, kinds)

        # 3. Analyse, within the day's OpenAI budget (a Melbourne calendar day).
        tracker = SpendTracker(settings.prices_per_million, settings.deep_model,
                               already_spent=ledger.spend_on(budget_day),
                               hard_limit=max(settings.daily_budget_usd * 1.5,
                                              settings.daily_budget_usd + 0.5))
        from .progress import ProgressPrinter
        progress = ProgressPrinter(spend_source=tracker)
        analyst = analyst or Analyst(settings, callbacks=[tracker], progress=progress)
        if getattr(analyst, "callbacks", None) is not None and tracker not in analyst.callbacks:
            analyst.callbacks.append(tracker)
        cash_for_bot = max(0.0, min(acct.cash, settings.capital_cap_usd - result.holdings_value))
        for n, inst in enumerate(todays, 1):
            if tracker.spent >= settings.daily_budget_usd:
                result.notes.append(f"daily OpenAI budget reached; skipped {inst.ticker}")
                continue
            progress.begin(inst.ticker, n, len(todays))
            portfolio = _portfolio_for(inst, settings, bot_qty, ledger, cash_for_bot)
            analysis_date = crypto_date(now) if inst.kind == "crypto" else session
            try:
                a = analyst.analyse(inst, analysis_date, portfolio)
            except BudgetExceeded as exc:
                result.notes.append(str(exc))
                a = Analysis(inst.ticker, "ERROR", "stopped: budget", "", str(exc))
            except Exception as exc:  # one bad ticker must not stop the rest
                log.exception("analysis failed for %s", inst.ticker)
                a = Analysis(inst.ticker, "ERROR", f"failed: {exc}", "", str(exc))
            result.analyses.append(a)
            ledger.log_rating(a.ticker, inst.kind, a.rating,
                              prices.get(inst.broker_symbol) if inst.tradable else None, now)
            ledger.data["spend"][budget_day] = round(tracker.spent, 6)  # running daily total
            ledger.save()
        result.spend_today = tracker.spent

        # 4. Plan orders.
        ratings = {a.ticker: a.rating for a in result.analyses if a.rating not in ("ERROR",)}
        pend = ledger.data["pending_orders"].values()
        busy = {o["symbol"] for o in pend}
        pending_buys = sum(max(0.0, o["qty"] - o.get("recorded_filled", 0.0)) * o["limit"]
                           for o in pend if o.get("side") == "buy")
        assets: dict[str, AssetInfo] = {}
        for inst in tradable:
            if inst.ticker in ratings and ratings[inst.ticker] not in ("Hold", "REVIEW"):
                try:
                    assets[inst.broker_symbol] = broker.asset(inst.broker_symbol)
                except BrokerError as exc:
                    result.notes.append(f"asset info {inst.broker_symbol}: {exc}")
        plan = build_plan(settings, ratings, tradable, bot_qty, prices, assets,
                          cash=acct.cash, busy_symbols=busy,
                          halted=bool(ledger.halted_reason), pending_buys_usd=pending_buys)
        result.plan = plan

        stopped = settings.stop_file.exists()
        if stopped:
            result.notes.append("STOP switch is on: no orders placed")

        # 5. Approve and place.
        if plan.orders and not dry_run and not stopped:
            timeout = settings.approval_timeout_minutes
            if kinds == {"crypto"}:
                timeout = min(timeout, 60)  # don't hold up the next crypto check
            approved = _approve(settings, plan, telegram, ledger, session, result, timeout)
            if settings.stop_file.exists():
                result.notes.append("STOP switch turned on while waiting: no orders placed")
                approved = []
            for idx, order in approved:
                cid = f"ta-{now.astimezone(MELB):%Y%m%d%H%M}-{idx}-{uuid.uuid4().hex[:8]}"
                try:
                    st = broker.submit_limit_order(order.symbol, order.side, order.qty,
                                                   order.limit_price, order.crypto, cid)
                    ledger.add_pending(st, {"symbol": order.symbol, "side": order.side,
                                            "qty": order.qty, "limit": order.limit_price,
                                            "ticker": order.ticker, "rating": order.rating})
                    result.submitted.append(order.describe())
                except BrokerError as exc:
                    result.failed.append(f"{order.describe()}: {exc}")
            ledger.save()
            if any(o.crypto for _, o in approved):
                sleep(3)
                _sync_orders(broker, ledger, result)  # crypto IOC orders settle at once

        if not dry_run and not only:  # a partial --only run must not block the scheduled run
            if kinds & {"us_stock", "asx"}:
                ledger.set_run_status(f"stocks:{session}", "done")
            if "crypto" in kinds:
                ledger.mark_run("crypto", now)
            if kinds & {"us_stock", "asx"}:
                ledger.mark_run("stocks", now)
        if not dry_run:
            value_now = sum(ledger.qty(s) * prices.get(s, 0.0) for s in ledger.holdings())
            ledger.add_snapshot(value_now, scope, now)
        ledger.save()

    from .report import send_summary, write_report
    result.report_path = str(write_report(settings, result, dry_run=dry_run))
    if _worth_a_message(result, kinds):
        send_summary(settings, telegram, result, dry_run=dry_run)
    return result


# -- helpers -----------------------------------------------------------------

def _worth_a_message(result: RunResult, kinds: set[str]) -> bool:
    """The daily stocks run always reports. A crypto-only check every few hours
    only messages you when something happened."""
    if kinds & {"us_stock", "asx"}:
        return True
    return bool(result.submitted or result.declined or result.failed or result.halted_now
                or any(a.rating == "ERROR" for a in result.analyses)
                or any("STOP" in n for n in result.notes))


def _notify(telegram: Telegram | None, text: str) -> None:
    if telegram:
        try:
            telegram.send(text)
        except TelegramError as exc:
            log.warning("telegram: %s", exc)


def _handle_commands(settings: Settings, ledger: Ledger, telegram: Telegram | None,
                     result: RunResult) -> None:
    if not telegram:
        return
    try:
        cmds, offset = telegram.read_commands(int(ledger.data.get("telegram_offset", 0)))
    except TelegramError as exc:
        result.notes.append(f"could not read Telegram commands: {exc}")
        return
    ledger.data["telegram_offset"] = offset
    for c in cmds:
        if c.text == "/stop":
            settings.stop_file.write_text("stopped from Telegram\n")
            result.notes.append("STOP switch turned on from Telegram")
        elif c.text == "/resume":
            settings.stop_file.unlink(missing_ok=True)
            ledger.clear_halt()
            result.notes.append("resumed from Telegram")
    ledger.save()


def _sync_orders(broker: AlpacaBroker, ledger: Ledger, result: RunResult) -> None:
    for oid in list(ledger.data["pending_orders"]):
        try:
            st = broker.get_order(oid)
        except BrokerError as exc:
            result.notes.append(f"order {oid[:8]}: {exc}")
            continue
        got = ledger.apply_order_update(st)
        if got:
            result.notes.append(f"filled {st.side} {got:.6g} {st.symbol} @ "
                                f"{fmt_price(st.filled_avg_price)}")
    ledger.save()


def _instruments_for_today(settings: Settings, broker: AlpacaBroker, session: str,
                           only: list[str] | None, bot_qty: dict[str, float],
                           result: RunResult, kinds: set[str]) -> list[Instrument]:
    weekday = datetime.fromisoformat(session).weekday()  # 0 = Monday
    us_open = None
    todays: list[Instrument] = []
    wanted = {t.upper() for t in only} if only else None
    for inst in settings.instruments:
        if wanted is not None:
            if inst.ticker not in wanted:
                continue
        elif inst.kind not in kinds:
            continue
        if inst.kind == "us_stock":
            if us_open is None:
                try:
                    us_open = broker.is_trading_day(session)
                except BrokerError as exc:
                    result.notes.append(f"calendar check failed ({exc}); assuming weekday rule")
                    us_open = weekday < 5
            if not us_open:
                continue
        elif inst.kind == "asx" and weekday >= 5:
            continue
        todays.append(inst)
    if wanted:
        unknown = wanted - {i.ticker for i in settings.instruments}
        if unknown:
            result.notes.append(f"not in watchlist: {', '.join(sorted(unknown))}")
    rank = {"us_stock": 1, "crypto": 2, "asx": 3}
    held = set(bot_qty)
    todays.sort(key=lambda i: (0 if i.broker_symbol in held else rank[i.kind]))
    return todays


def _portfolio_for(inst: Instrument, settings: Settings, bot_qty: dict[str, float],
                   ledger: Ledger, cash_for_bot: float) -> dict | None:
    if not inst.tradable:
        return None  # ASX: the bot has no view of your Stake account
    by_symbol = {i.broker_symbol: i.ticker for i in settings.instruments if i.tradable}
    positions = []
    for sym, q in bot_qty.items():
        if q <= 0:
            continue
        h = ledger.data["holdings"].get(sym, {})
        avg = h.get("cost", 0.0) / h["qty"] if h.get("qty") else None
        positions.append({"ticker": by_symbol.get(sym, sym), "quantity": q,
                          "average_price": avg})
    return {"cash": round(cash_for_bot, 2), "currency": "USD", "positions": positions}


def _approve(settings: Settings, plan: Plan, telegram: Telegram | None, ledger: Ledger,
             session: str, result: RunResult, timeout_minutes: int):
    indexed = list(enumerate(plan.orders))
    if not settings.needs_approval:
        return indexed
    if not telegram:
        result.notes.append("approval needed but Telegram is not set up: no orders placed")
        return []
    plan_id = uuid.uuid4().hex[:6]
    header = (f"<b>Trade plan · {settings.mode.upper()} · {result.scope}</b>\n"
              f"Bot holdings ${plan.holdings_value:,.2f} of ${settings.capital_cap_usd:,.0f} cap. "
              f"Tap Approve or Skip on each order. Anything unanswered in "
              f"{timeout_minutes} min is skipped.")
    try:
        decisions, offset = telegram.ask_approval(
            plan_id, [o.describe() for o in plan.orders], header,
            int(ledger.data.get("telegram_offset", 0)), timeout_minutes)
    except TelegramError as exc:
        result.notes.append(f"approval failed ({exc}): no orders placed")
        return []
    ledger.data["telegram_offset"] = offset
    if getattr(telegram, "stop_requested", False):
        settings.stop_file.write_text("stopped from Telegram during approval\n")
        result.notes.append("STOP switch turned on from Telegram")
    ledger.save()
    approved = []
    for i, o in indexed:
        if decisions.get(i):
            approved.append((i, o))
        else:
            result.declined.append(o.describe())
    return approved
