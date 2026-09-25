import random

from conftest import PRICES, asset

from agent.planner import build_plan

ASSETS = {s: asset(s) for s in PRICES}


def plan_for(s, ratings, held=None, cash=1000.0, halted=False, busy=None, assets=None):
    return build_plan(s, ratings, s.instruments, held or {}, PRICES, assets or ASSETS,
                      cash=cash, busy_symbols=busy, halted=halted)


def test_buy_sizes_to_ten_percent_of_cap(make_settings):
    s = make_settings()
    p = plan_for(s, {"NVDA": "Buy"})
    [o] = p.orders
    assert o.side == "buy" and o.symbol == "NVDA"
    assert o.limit_price == 181.8          # 1% above 180, rounded to the cent
    assert 49 <= o.est_notional <= 50.0    # never above the $50 target
    assert o.qty == round(o.qty, 6)


def test_overweight_is_half_size(make_settings):
    s = make_settings()
    [o] = plan_for(s, {"ETH-USD": "Overweight"}).orders
    assert o.symbol == "ETH/USD" and o.crypto
    assert 24 <= o.est_notional <= 25.0


def test_buy_when_already_at_target_does_nothing(make_settings):
    s = make_settings()
    held = {"NVDA": 49.0 / 180}  # $49 held vs $50 target
    assert plan_for(s, {"NVDA": "Buy"}, held).orders == []


def test_buy_tops_up_to_target(make_settings):
    s = make_settings()
    held = {"NVDA": 20.0 / 180}
    [o] = plan_for(s, {"NVDA": "Buy"}, held).orders
    assert 29 <= o.est_notional <= 30.0


def test_sell_and_underweight(make_settings):
    s = make_settings()
    held = {"NVDA": 0.3, "BTC/USD": 0.0004}
    p = plan_for(s, {"NVDA": "Sell", "BTC-USD": "Underweight"}, held)
    by = {o.symbol: o for o in p.orders}
    assert by["NVDA"].side == "sell" and by["NVDA"].qty == 0.3
    # Underweight trims $44 of BTC down to the $12.50 underweight target
    assert by["BTC/USD"].side == "sell"
    assert abs(by["BTC/USD"].qty - (0.0004 * 110_000 - 12.5) / 110_000) < 1e-8
    assert by["NVDA"].limit_price == 178.2  # 1% below


def test_sell_without_holding_does_nothing(make_settings):
    s = make_settings()
    assert plan_for(s, {"NVDA": "Sell", "ETH-USD": "Underweight"}).orders == []


def test_hold_and_review_do_nothing(make_settings):
    s = make_settings()
    assert plan_for(s, {"NVDA": "Hold", "AAPL": "REVIEW"}, {"NVDA": 0.1}).orders == []


def test_asx_is_never_traded(make_settings):
    s = make_settings()
    assert plan_for(s, {"NDQ.AX": "Buy", "CBA.AX": "Sell", "BHP.AX": "Buy"}).orders == []


def test_capital_cap_limits_buys(make_settings):
    s = make_settings()
    held = {"NVDA": 480.0 / 180}   # $480 of the $500 cap used
    p = plan_for(s, {"AAPL": "Buy", "MSFT": "Buy"}, held)
    total = sum(o.est_notional for o in p.orders if o.side == "buy")
    assert total <= 20.0 + 1e-9
    assert any("capital cap" in x for x in p.skipped)


def test_cash_limits_buys(make_settings):
    s = make_settings()
    p = plan_for(s, {"AAPL": "Buy"}, cash=12.0)
    [o] = p.orders
    assert o.est_notional <= 12.0


def test_no_cash_no_buy(make_settings):
    s = make_settings()
    p = plan_for(s, {"AAPL": "Buy"}, cash=3.0)
    assert p.orders == [] and any("cash" in x for x in p.skipped)


def test_halted_blocks_buys_but_allows_sells(make_settings):
    s = make_settings()
    p = plan_for(s, {"AAPL": "Buy", "NVDA": "Sell"}, {"NVDA": 0.2}, halted=True)
    assert [o.side for o in p.orders] == ["sell"]


def test_busy_symbol_skipped(make_settings):
    s = make_settings()
    p = plan_for(s, {"AAPL": "Buy"}, busy={"AAPL"})
    assert p.orders == [] and "still open" in p.skipped[0]


def test_order_limit_keeps_sells_first(make_settings):
    s = make_settings()
    ratings = {t: "Buy" for t in ["NVDA", "AVGO", "MSFT", "AAPL", "ABBV",
                                  "BTC-USD", "ETH-USD", "SOL-USD"]}
    ratings["NVDA"] = "Sell"
    p = plan_for(s, ratings, {"NVDA": 0.1})
    assert len(p.orders) == 6
    assert p.orders[0].side == "sell"
    assert any("order limit" in x for x in p.skipped)


def test_non_fractionable_uses_whole_shares(make_settings):
    s = make_settings()
    assets = dict(ASSETS, MSFT=asset("MSFT", fractionable=False))
    p = plan_for(s, {"MSFT": "Buy"}, assets=assets)  # $510 share, $50 budget
    assert p.orders == [] and "minimum" in p.skipped[0]


def test_random_invariants(make_settings):
    s = make_settings()
    rng = random.Random(7)
    ratings_pool = ["Buy", "Overweight", "Hold", "Underweight", "Sell", "REVIEW"]
    tickers = [i.ticker for i in s.instruments]
    sym = {i.ticker: i.broker_symbol for i in s.instruments}
    for _ in range(500):
        ratings = {t: rng.choice(ratings_pool) for t in tickers}
        held = {sym[t]: rng.uniform(0, 80) / PRICES[sym[t]]
                for t in tickers if sym[t] and rng.random() < 0.5}
        cash = rng.uniform(0, 600)
        p = plan_for(s, ratings, held, cash=cash)
        held_value = sum(q * PRICES[k] for k, q in held.items())
        buys = [o for o in p.orders if o.side == "buy"]
        spend = sum(o.qty * o.limit_price for o in buys)
        assert spend <= max(0.0, 500 - held_value) + 1e-6       # never above the cap
        assert spend <= cash + 1e-6                              # never on margin
        assert len(p.orders) <= s.max_orders_per_run
        for o in p.orders:
            assert o.symbol is not None
            if o.side == "sell":
                assert o.qty <= held.get(o.symbol, 0) + 1e-12       # never short
                assert ratings[o.ticker] in ("Sell", "Underweight")
            else:
                assert ratings[o.ticker] in ("Buy", "Overweight")
                target = (0.10 if ratings[o.ticker] == "Buy" else 0.05) * 500
                assert held.get(o.symbol, 0) * PRICES[o.symbol] + o.qty * PRICES[o.symbol] \
                    <= target + 1e-6


def test_pending_buys_count_against_cap_and_cash(make_settings):
    s = make_settings()
    p = build_plan(s, {"AAPL": "Buy"}, s.instruments, {}, PRICES, ASSETS, cash=1000,
                   pending_buys_usd=470)
    [o] = p.orders
    assert o.est_notional <= 30 + 1e-9


def test_underweight_is_stable_across_repeated_runs(make_settings):
    """Crypto is checked every few hours; Underweight must not keep halving."""
    s = make_settings()
    held = {"ETH/USD": 12.0 / PRICES["ETH/USD"]}  # already at/below the $12.50 target
    assert plan_for(s, {"ETH-USD": "Underweight"}, held).orders == []
    held = {"ETH/USD": 16.0 / PRICES["ETH/USD"]}  # within min_order of target: leave it
    assert plan_for(s, {"ETH-USD": "Underweight"}, held).orders == []
