from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from conftest import PRICES, FakeAnalyst, FakeBroker, FakeTelegram

from agent.ledger import Ledger
from agent.runner import run
from agent.weekly import build_weekly, render_markdown, render_telegram, run_weekly

NY = ZoneInfo("America/New_York")
MELB = ZoneInfo("Australia/Melbourne")
MONDAY = datetime(2026, 9, 28, 18, 30, tzinfo=NY)
FRIDAY_6PM = datetime(2026, 10, 2, 18, 0, tzinfo=MELB)


def _week_of_runs(s, b):
    run(s, broker=b, analyst=FakeAnalyst({"BTC-USD": "Buy", "NVDA": "Overweight",
                                          "CBA.AX": "Underweight"}),
        telegram=False, now=MONDAY, sleep=lambda _: None)
    b.fill_open_orders()
    run(s, broker=b, analyst=FakeAnalyst({"BTC-USD": "Sell", "NVDA": "Hold"}),
        telegram=False, now=MONDAY + timedelta(days=1), sleep=lambda _: None)


def test_weekly_summary_contents(make_settings):
    s = make_settings()
    b = FakeBroker()
    _week_of_runs(s, b)
    led = Ledger(s.state_path)
    w = build_weekly(s, led, b, now=FRIDAY_6PM)
    assert w["runs"] == 2
    assert len(w["trades"]) >= 3              # BTC buy, NVDA fill, BTC sell
    assert "NVDA" in w["holdings"] and "BTC/USD" not in w["holdings"]
    assert dict(w["bench"])["Bitcoin"] is not None
    assert abs(dict(w["bench"])["Bitcoin"] - 0.05) < 1e-9
    assert w["latest"]["BTC-USD"] == "Sell"
    assert w["changes"]["BTC-USD"] == ["Buy→Sell"]
    assert len(w["groups"]["bullish"]) == 2 and len(w["groups"]["bearish"]) == 1

    md = render_markdown(s, w)
    for part in ("Weekly summary", "## Holdings", "## Trades this week", "Bitcoin +5.0%",
                 "Latest rating per ticker", "CBA.AX", "ASX (signal)"):
        assert part in md, part
    tg = render_telegram(s, w)
    assert "Weekly summary" in tg and "CBA.AX Hold" in tg
    assert w["changes"]["CBA.AX"] == ["Underweight→Hold"]


def test_week_pnl_uses_snapshot_from_before_the_week(make_settings):
    s = make_settings()
    b = FakeBroker()
    led = Ledger(s.state_path)
    led.data["snapshots"] = [{"time": "2026-09-20T00:00:00+00:00", "scope": "all",
                              "value": 0, "net_invested": 0, "pnl": -7.5}]
    led.save()
    w = build_weekly(s, Ledger(s.state_path), b, now=FRIDAY_6PM)
    assert w["pnl_now"] == 0 and w["pnl_week"] == 7.5


def test_run_weekly_saves_and_sends(make_settings):
    s = make_settings(telegram=True)
    b = FakeBroker()
    _week_of_runs(s, b)
    t = FakeTelegram()
    path, md = run_weekly(s, broker=b, telegram=t, now=FRIDAY_6PM)
    assert path.exists() and path.parent.name == "weekly" and "2026-10-02" in path.name
    assert len(t.sent) == 1 and "Weekly summary" in t.sent[0]


def test_spend_is_summed_over_the_week(make_settings):
    s = make_settings()
    led = Ledger(s.state_path)
    led.data["spend"] = {"2026-09-20": 9.0, "2026-09-28": 0.5, "2026-10-01": 0.25}
    led.save()
    w = build_weekly(s, Ledger(s.state_path), FakeBroker(), now=FRIDAY_6PM)
    assert abs(w["spend"] - 0.75) < 1e-9
    assert PRICES["SPY"] > 0
