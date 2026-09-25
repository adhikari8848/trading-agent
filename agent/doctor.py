"""Check every connection the agent needs, without trading or spending."""

from __future__ import annotations

import sys

import requests

from .ci import annotate
from .settings import Settings


def _line(ok: bool | None, name: str, detail: str, public: str | None = None) -> bool:
    """Print a check result. On GitHub it also becomes an annotation; `public` is the
    text used there when `detail` holds anything that shouldn't be shown publicly."""
    mark = {True: "OK  ", False: "FAIL", None: "SKIP"}[ok]
    print(f"[{mark}] {name}: {detail}")
    shown = public if public is not None else detail
    annotate({True: "notice", False: "error", None: "notice"}[ok],
             f"{mark.strip()} {name}", shown)
    return ok is not False


def run_doctor(s: Settings) -> bool:
    good = True
    v = sys.version_info
    good &= _line(v >= (3, 10), "Python", f"{v.major}.{v.minor}.{v.micro}")

    try:
        import tradingagents  # noqa: F401
        from tradingagents.graph.trading_graph import TradingAgentsGraph  # noqa: F401
        good &= _line(True, "TradingAgents", "installed")
    except Exception as exc:
        good &= _line(False, "TradingAgents", f"import failed: {exc}")

    print(f"       mode={s.mode}, cap=${s.capital_cap_usd:,.0f}, "
          f"models={s.deep_model}/{s.quick_model}, "
          f"watchlist={', '.join(i.ticker for i in s.instruments)}")

    # OpenAI: list models (free call) and confirm the configured ones exist
    if not s.openai_api_key:
        good &= _line(False, "OpenAI", "OPENAI_API_KEY missing (.env on a Mac, repo secret on GitHub)")
    else:
        try:
            r = requests.get("https://api.openai.com/v1/models", timeout=20,
                             headers={"Authorization": f"Bearer {s.openai_api_key}"})
            if r.status_code == 200:
                ids = {m["id"] for m in r.json().get("data", [])}
                missing = [m for m in (s.deep_model, s.quick_model) if m not in ids]
                good &= _line(not missing, "OpenAI",
                              "key works" + (f", but no access to {missing}" if missing else
                                             f", {s.deep_model} and {s.quick_model} available"))
            else:
                good &= _line(False, "OpenAI", f"HTTP {r.status_code}: {r.text[:150]}")
        except requests.RequestException as exc:
            good &= _line(False, "OpenAI", str(exc))

    # Alpaca
    from .broker import AlpacaBroker, BrokerError
    label = "Alpaca (LIVE)" if s.is_live else "Alpaca (paper)"
    if not (s.alpaca_key_id and s.alpaca_secret):
        good &= _line(False, label, "keys missing (.env on a Mac, repo secrets on GitHub)")
    else:
        try:
            b = AlpacaBroker(s.alpaca_key_id, s.alpaca_secret, live=s.is_live)
            a = b.account()
            ok = a.status == "ACTIVE" and not a.trading_blocked
            good &= _line(ok, label, f"status {a.status}, cash ${a.cash:,.2f}, "
                                     f"equity ${a.equity:,.2f} {a.currency}")
            try:
                px = b.latest_price("BTC/USD", crypto=True)
                _line(True, "Alpaca market data", f"BTC/USD ${px:,.0f}")
            except BrokerError as exc:
                good &= _line(False, "Alpaca market data", str(exc))
        except BrokerError as exc:
            good &= _line(False, label, str(exc))

    # Yahoo Finance (what TradingAgents reads prices and news from)
    try:
        import yfinance as yf
        h = yf.Ticker("AAPL").history(period="5d")
        good &= _line(not h.empty, "Yahoo Finance", f"{len(h)} days of AAPL prices")
    except Exception as exc:
        good &= _line(False, "Yahoo Finance", str(exc))

    # Telegram
    if not s.telegram_token:
        needed = s.is_live or s.approval_in_paper
        good &= _line(False if needed else None, "Telegram",
                      "not set up" + (" (required for live mode)" if needed else " (optional in paper mode)"))
    else:
        try:
            r = requests.get(f"https://api.telegram.org/bot{s.telegram_token}/getMe", timeout=15)
            d = r.json()
            if d.get("ok"):
                if s.telegram_chat_id:
                    good &= _line(True, "Telegram", f"bot @{d['result']['username']}, chat {s.telegram_chat_id}",
                                  public="bot token and chat id work")
                else:
                    good &= _line(False, "Telegram", "token works but TELEGRAM_CHAT_ID missing: "
                                                     "run  python -m agent telegram-setup")
            else:
                good &= _line(False, "Telegram", d.get("description", "bad token"))
        except (requests.RequestException, ValueError) as exc:
            good &= _line(False, "Telegram", str(exc))

    if s.stop_file.exists():
        print("[NOTE] STOP switch is on: runs will analyse but not trade. "
              "Remove with  python -m agent resume")
    print("\nAll good." if good else "\nFix the FAIL lines above, then run doctor again.")
    return bool(good)
