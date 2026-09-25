"""Command line: python -m agent <command>

  doctor            check keys and connections (no trading, no spend)
  run               analyse, plan, approve, trade, report (--scope stocks|crypto|all)
  weekly            build the weekly summary and send it to Telegram
  status            what the bot holds, P&L, open orders, recent spend
  stop / resume     kill switch: stop all trading / allow it again
  telegram-setup    find your Telegram chat id and save it to .env
  install-schedule  install the automatic runs from config.yaml > schedule (macOS)
  remove-schedule   stop all automatic runs
"""

from __future__ import annotations

import argparse
import logging
import sys
from logging.handlers import RotatingFileHandler

from .settings import PROJECT_DIR, SettingsError, load_settings


def _logging(verbose: bool) -> None:
    (PROJECT_DIR / "logs").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = RotatingFileHandler(PROJECT_DIR / "logs" / "agent.log", maxBytes=5_000_000, backupCount=5)
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    sh.setLevel(logging.INFO if verbose else logging.WARNING)
    sh.addFilter(lambda r: r.name != "agent.progress")  # already printed to the screen
    if not verbose:
        # Optional data sources that are unavailable (FRED without a key, Polymarket,
        # which is blocked in Australia) warn on every call. Keep those in the log file only.
        sh.addFilter(lambda r: not (r.name.startswith("tradingagents.dataflows")
                                    and r.levelno < logging.ERROR))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(fh)
    root.addHandler(sh)


def _raise_open_file_limit() -> None:
    """macOS allows only 256 open files per process by default. A full run opens
    many HTTP and cache handles and can hit that (yfinance then reports 'unable to
    open database file'). Raise the soft limit for this process only."""
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = 4096 if hard == resource.RLIM_INFINITY else min(4096, hard)
        if soft < target:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    except (ImportError, ValueError, OSError):
        pass


def main(argv: list[str] | None = None) -> int:
    _raise_open_file_limit()
    p = argparse.ArgumentParser(prog="python -m agent", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor")
    r = sub.add_parser("run")
    r.add_argument("--dry-run", action="store_true", help="analyse and plan, but place no orders")
    r.add_argument("--only", help="comma-separated tickers from the watchlist, e.g. NVDA,BTC-USD")
    r.add_argument("--force", action="store_true", help="run even if this run already happened")
    r.add_argument("--scope", choices=["all", "stocks", "crypto"], default="all",
                   help="stocks = US stocks + ASX signals, crypto = BTC/ETH/SOL (default: all)")
    r.add_argument("--wait", type=float, default=0, metavar="MIN",
                   help="if another run is going, wait up to MIN minutes for it")
    r.add_argument("-v", "--verbose", action="store_true")
    sub.add_parser("status")
    sub.add_parser("stop")
    sub.add_parser("resume")
    sub.add_parser("telegram-setup")
    wk = sub.add_parser("weekly")
    wk.add_argument("--no-send", action="store_true", help="save the report but don't message")
    sub.add_parser("install-schedule")
    sub.add_parser("remove-schedule")
    args = p.parse_args(argv)

    if args.cmd == "remove-schedule":
        from .schedule import uninstall
        uninstall()
        return 0
    if args.cmd == "telegram-setup":
        from .setup_telegram import setup_telegram
        return setup_telegram()

    try:
        s = load_settings()
    except SettingsError as exc:
        print(f"Config problem: {exc}")
        return 2

    if args.cmd == "install-schedule":
        from .schedule import install
        install(s)
        return 0
    if args.cmd == "weekly":
        _logging(False)
        from .weekly import run_weekly
        path, md = run_weekly(s, send=not args.no_send)
        print(md)
        print(f"Saved: {path}")
        return 0
    if args.cmd == "doctor":
        from .doctor import run_doctor
        return 0 if run_doctor(s) else 1
    if args.cmd == "stop":
        s.data_dir.mkdir(parents=True, exist_ok=True)
        s.stop_file.write_text("stopped from the command line\n")
        print("STOP switch on. Runs will still analyse and report, but place no orders.")
        return 0
    if args.cmd == "resume":
        from .ledger import Ledger
        s.stop_file.unlink(missing_ok=True)
        led = Ledger(s.state_path)
        led.clear_halt()
        led.save()
        print("Trading allowed again.")
        return 0
    if args.cmd == "status":
        from .status import print_status
        return print_status(s)

    _logging(getattr(args, "verbose", False))
    from .runner import RunError, run
    only = [t.strip() for t in args.only.split(",")] if args.only else None
    try:
        res = run(s, scope=args.scope, dry_run=args.dry_run, only=only, force=args.force,
                  wait_minutes=args.wait)
    except RunError as exc:
        logging.getLogger("agent").error("%s", exc)
        print(f"Run stopped: {exc}")
        return 1
    for a in res.analyses:
        print(f"{a.ticker:10s} {a.rating:12s} {a.summary[:100]}")
    for line in res.submitted:
        print(f"PLACED   {line}")
    for line in res.declined:
        print(f"SKIPPED  {line}")
    for line in res.failed:
        print(f"FAILED   {line}")
    if args.dry_run and res.plan:
        for o in res.plan.orders:
            print(f"WOULD    {o.describe()}")
    for n in res.notes:
        print(f"note: {n}")
    if res.ran:
        print(f"OpenAI spend today: ${res.spend_today:.2f}")
    if res.report_path:
        print(f"Report: {res.report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
