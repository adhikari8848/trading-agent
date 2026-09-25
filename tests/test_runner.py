from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from conftest import FakeAnalyst, FakeBroker, FakeTelegram

from agent.ledger import Ledger
from agent.runner import RunError, run

NY = ZoneInfo("America/New_York")
MONDAY = datetime(2026, 9, 28, 18, 30, tzinfo=NY)    # 8:30am Tue in Melbourne
SATURDAY = datetime(2026, 9, 26, 18, 30, tzinfo=NY)


def go(s, broker, analyst, telegram=None, **kw):  # noqa: D103
    return run(s, broker=broker, analyst=analyst, telegram=telegram or False,
               now=kw.pop("now", MONDAY), sleep=lambda _: None, **kw)


def test_paper_run_places_orders_automatically(make_settings):
    s = make_settings("paper")
    b = FakeBroker(cash=1000)
    a = FakeAnalyst({"NVDA": "Buy", "BTC-USD": "Overweight", "NDQ.AX": "Buy"})
    go(s, b, a)
    assert len(a.calls) == 11                        # 5 US + 3 crypto + 3 ASX
    assert {x[0] for x in b.submitted} == {"NVDA", "BTC/USD"}  # ASX never traded
    led = Ledger(s.state_path)
    assert led.qty("BTC/USD") > 0                     # crypto IOC filled and booked
    assert len(led.data["pending_orders"]) == 1       # NVDA waits for the US open
    assert led.run_status("stocks:2026-09-28") == "done"
    assert led.last_run("crypto") and led.last_run("stocks")
    assert (s.reports_dir / "2026-09-28.md").exists()

    # next day: the queued NVDA order has filled at the open
    b.fill_open_orders()
    a2 = FakeAnalyst({"NVDA": "Sell"})
    res2 = go(s, b, a2, now=MONDAY.replace(day=29))
    led = Ledger(s.state_path)
    assert any(x[0] == "NVDA" and x[1] == "sell" for x in b.submitted)
    assert any("filled buy" in n for n in res2.notes)
    # the bot passed its own NVDA position to the analysts
    nvda_portfolio = dict(a2.calls)["NVDA"]
    assert "NVDA" in {x["ticker"] for x in nvda_portfolio["positions"]}
    assert "BTC-USD" in {x["ticker"] for x in nvda_portfolio["positions"]}


def test_asx_signals_get_no_portfolio(make_settings):
    s = make_settings()
    a = FakeAnalyst({})
    go(s, FakeBroker(), a)
    assert dict(a.calls)["NDQ.AX"] is None
    assert dict(a.calls)["NVDA"]["cash"] == 500.0   # capped at the $500 limit, not $1000


def test_live_requires_telegram(make_settings):
    s = make_settings("live")
    with pytest.raises(RunError, match="Telegram"):
        go(s, FakeBroker(), FakeAnalyst({}))


def test_live_places_only_approved_orders(make_settings):
    s = make_settings("live", telegram=True)
    b = FakeBroker()
    t = FakeTelegram(approve={0})
    res = go(s, b, FakeAnalyst({"NVDA": "Buy", "AAPL": "Buy"}), telegram=t)
    assert len(t.asked[0]) == 2
    assert len(b.submitted) == 1 and len(res.declined) == 1
    assert any("LIVE" in m for m in t.sent)


def test_stop_switch_blocks_orders(make_settings):
    s = make_settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    s.stop_file.write_text("x")
    b = FakeBroker()
    res = go(s, b, FakeAnalyst({"NVDA": "Buy"}))
    assert b.submitted == [] and any("STOP" in n for n in res.notes)


def test_telegram_stop_command(make_settings):
    s = make_settings("live", telegram=True)
    b = FakeBroker()
    t = FakeTelegram(commands=["/stop"])
    go(s, b, FakeAnalyst({"NVDA": "Buy"}), telegram=t)
    assert b.submitted == [] and s.stop_file.exists()
    t2 = FakeTelegram(commands=["/resume"])
    go(s, b, FakeAnalyst({"NVDA": "Buy"}), telegram=t2, force=True)
    assert not s.stop_file.exists() and len(b.submitted) == 1


def test_dry_run_places_nothing_and_can_rerun(make_settings):
    s = make_settings()
    b = FakeBroker()
    res = go(s, b, FakeAnalyst({"NVDA": "Buy"}), dry_run=True)
    assert b.submitted == [] and res.plan.orders
    assert Ledger(s.state_path).run_status("stocks:2026-09-28") is None


def test_second_run_same_day_is_skipped(make_settings):
    s = make_settings()
    go(s, FakeBroker(), FakeAnalyst({}))
    a = FakeAnalyst({})
    res = go(s, FakeBroker(), a)
    assert a.calls == [] and not res.ran and "nothing due" in res.notes[0]


def test_weekend_skips_stocks(make_settings):
    s = make_settings()
    a = FakeAnalyst({})
    go(s, FakeBroker(trading_day=False), a, now=SATURDAY)
    assert [c[0] for c in a.calls] == ["BTC-USD", "ETH-USD", "SOL-USD"]


def test_daily_budget_stops_analysis(make_settings):
    s = make_settings()
    a = FakeAnalyst({}, cost_per_call=1.2)
    res = go(s, FakeBroker(), a)
    assert len(a.calls) == 3                     # 3.6 spent > 3.0 budget
    assert sum("budget" in n for n in res.notes) == 8
    # budget is per Melbourne day: 6:30pm Mon New York = Tue 29 Sep Melbourne
    assert abs(Ledger(s.state_path).spend_on("2026-09-29") - 3.6) < 1e-9


def test_failed_ticker_does_not_stop_run(make_settings):
    s = make_settings()
    b = FakeBroker()
    res = go(s, b, FakeAnalyst({"AAPL": "Buy"}, fail={"NVDA"}))
    assert {a.ticker: a.rating for a in res.analyses}["NVDA"] == "ERROR"
    assert [x[0] for x in b.submitted] == ["AAPL"]


def test_loss_limit_halts_buying(make_settings):
    s = make_settings("live", telegram=True)
    led = Ledger(s.state_path)
    led.data["holdings"] = {"NVDA": {"qty": 0.5, "cost": 200.0}}
    led.data["net_invested"] = 200.0      # now worth 0.5 * 180 = $90 -> P&L -$110
    led.save()
    b = FakeBroker(positions={"NVDA": 0.5})
    t = FakeTelegram()
    go(s, b, FakeAnalyst({"AAPL": "Buy", "NVDA": "Sell"}), telegram=t)
    assert [x[1] for x in b.submitted] == ["sell"]
    assert any("halted" in m for m in t.sent)
    assert Ledger(s.state_path).halted_reason


def test_never_sells_your_own_shares(make_settings):
    s = make_settings()
    b = FakeBroker(positions={"AAPL": 3.0})   # bought by you, not the bot
    go(s, b, FakeAnalyst({"AAPL": "Sell"}))
    assert b.submitted == []


def test_only_run_does_not_block_daily_run(make_settings):
    s = make_settings()
    go(s, FakeBroker(), FakeAnalyst({}), only=["NVDA"])
    a = FakeAnalyst({})
    go(s, FakeBroker(), a)
    assert len(a.calls) == 11


def test_paper_and_live_books_are_separate(make_settings):
    paper = make_settings("paper")
    go(paper, FakeBroker(), FakeAnalyst({"BTC-USD": "Buy"}))
    live = make_settings("live", telegram=True)
    assert live.state_path != paper.state_path
    assert Ledger(paper.state_path).holdings() and not Ledger(live.state_path).holdings()


def test_session_date_is_last_completed_us_session():
    from agent.runner import session_date
    melb = ZoneInfo("Australia/Melbourne")
    # 8:30am Saturday Melbourne = Friday 6:30pm New York -> Friday's session
    assert session_date(datetime(2026, 9, 26, 8, 30, tzinfo=melb)) == "2026-09-25"
    # 2:30pm Friday Melbourne = 12:30am Friday New York -> Thursday's session
    assert session_date(datetime(2026, 9, 25, 14, 30, tzinfo=melb)) == "2026-09-24"
    # summer time: 8:30am Wednesday Melbourne (AEDT) = 4:30pm Tuesday New York (EST)
    assert session_date(datetime(2026, 12, 2, 8, 30, tzinfo=melb)) == "2026-12-01"


def test_second_report_same_day_does_not_overwrite(make_settings):
    s = make_settings()
    go(s, FakeBroker(), FakeAnalyst({}))
    go(s, FakeBroker(), FakeAnalyst({}), force=True)
    assert len(list(s.reports_dir.glob("2026-09-28*.md"))) == 2


def test_crypto_runs_every_few_hours_stocks_once_a_day(make_settings):
    from datetime import timedelta
    s = make_settings()
    b = FakeBroker()
    go(s, b, FakeAnalyst({}), scope="stocks")                  # 8:30am: stocks + ASX
    a = FakeAnalyst({})
    go(s, b, a, scope="stocks", now=MONDAY + timedelta(hours=1))
    assert a.calls == []                                        # stocks: once per session
    a = FakeAnalyst({})
    go(s, b, a, scope="crypto", now=MONDAY + timedelta(hours=1))
    assert [c[0] for c in a.calls] == ["BTC-USD", "ETH-USD", "SOL-USD"]
    a = FakeAnalyst({})
    go(s, b, a, scope="crypto", now=MONDAY + timedelta(hours=2))
    assert a.calls == []                                        # not due yet (4h interval)
    a = FakeAnalyst({})
    go(s, b, a, scope="crypto", now=MONDAY + timedelta(hours=5))
    assert len(a.calls) == 3                                    # due again
    led = Ledger(s.state_path)
    assert len(led.data["snapshots"]) == 3
    assert {r["kind"] for r in led.data["ratings_log"]} == {"us_stock", "asx", "crypto"}


def test_crypto_scope_on_a_weekend(make_settings):
    s = make_settings()
    a = FakeAnalyst({"BTC-USD": "Buy"})
    b = FakeBroker(trading_day=False)
    res = go(s, b, a, scope="crypto", now=SATURDAY)
    assert [c[0] for c in a.calls] == ["BTC-USD", "ETH-USD", "SOL-USD"]
    assert [x[0] for x in b.submitted] == ["BTC/USD"]
    assert res.report_path and "/crypto/" in res.report_path


def test_quiet_crypto_checks_do_not_message(make_settings):
    from datetime import timedelta
    s = make_settings(telegram=True)
    t = FakeTelegram()
    go(s, FakeBroker(), FakeAnalyst({}), telegram=t, scope="crypto")
    assert t.sent == []                                          # nothing happened: no ping
    t2 = FakeTelegram()
    go(s, FakeBroker(), FakeAnalyst({"ETH-USD": "Buy"}), telegram=t2, scope="crypto",
       now=MONDAY + timedelta(hours=4))
    assert len(t2.sent) == 1                                     # traded: summary sent
    t3 = FakeTelegram()
    go(s, FakeBroker(), FakeAnalyst({}), telegram=t3, scope="stocks")
    assert len(t3.sent) == 1                                     # daily stocks run always reports


def test_legacy_done_key_counts_for_stocks(make_settings):
    s = make_settings()
    led = Ledger(s.state_path)
    led.set_run_status("2026-09-28", "done")      # written by the first version
    led.save()
    a = FakeAnalyst({})
    go(s, FakeBroker(), a, scope="stocks")
    assert a.calls == []


def test_scheduled_run_waits_for_lock(make_settings):
    import fcntl
    s = make_settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    holder = open(s.data_dir / ".run.lock", "w")
    fcntl.flock(holder, fcntl.LOCK_EX)
    waits = []

    def fake_sleep(sec):
        waits.append(sec)
        if len(waits) == 2:
            fcntl.flock(holder, fcntl.LOCK_UN)   # the other run finishes

    res = run(s, broker=FakeBroker(), analyst=FakeAnalyst({}), telegram=False, now=MONDAY,
              sleep=fake_sleep, wait_minutes=5, scope="crypto")
    assert res.ran and len(waits) >= 2
    holder.close()


def test_mac_copy_refuses_to_trade_after_moving_to_github(make_settings, monkeypatch):
    s = make_settings()
    (s.project_dir / ".runs-on-github").touch()
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("TA_ALLOW_LOCAL", raising=False)
    b = FakeBroker()
    with pytest.raises(RunError, match="GitHub Actions"):
        go(s, b, FakeAnalyst({"NVDA": "Buy"}))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")        # the cloud copy is allowed
    go(s, b, FakeAnalyst({"NVDA": "Buy"}))
    assert b.submitted


def test_stop_switch_lives_in_data_dir(make_settings):
    s = make_settings()
    assert s.stop_file.parent == s.data_dir               # persists on the state branch
