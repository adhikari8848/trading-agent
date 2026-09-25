from agent.broker import OrderStatus
from agent.ledger import Ledger


def order(oid, side, filled, price, status="filled", sym="NVDA"):
    return OrderStatus(oid, "ta-x", sym, side, status, filled, price)


def test_buy_then_sell_books_pnl(tmp_path):
    led = Ledger(tmp_path / "s.json")
    led.add_pending(order("1", "buy", 0, None, "new"), {"symbol": "NVDA"})
    led.apply_order_update(order("1", "buy", 0.5, 100.0))
    assert led.qty("NVDA") == 0.5 and led.data["net_invested"] == 50.0
    assert led.data["pending_orders"] == {}

    led.add_pending(order("2", "sell", 0, None, "new"), {"symbol": "NVDA"})
    led.apply_order_update(order("2", "sell", 0.25, 120.0))
    assert led.qty("NVDA") == 0.25
    assert abs(led.data["realized_pnl"] - 5.0) < 1e-9
    assert abs(led.pnl({"NVDA": 0.25 * 120}) - 10.0) < 1e-9
    led.save()
    again = Ledger(tmp_path / "s.json")
    assert again.qty("NVDA") == 0.25


def test_partial_fills_are_booked_once(tmp_path):
    led = Ledger(tmp_path / "s.json")
    led.add_pending(order("1", "buy", 0, None, "new"), {"symbol": "NVDA"})
    led.apply_order_update(order("1", "buy", 0.2, 100.0, "partially_filled"))
    led.apply_order_update(order("1", "buy", 0.2, 100.0, "partially_filled"))
    assert led.qty("NVDA") == 0.2
    led.apply_order_update(order("1", "buy", 0.5, 100.0, "filled"))
    assert abs(led.qty("NVDA") - 0.5) < 1e-12
    assert "1" not in led.data["pending_orders"]


def test_unknown_order_is_ignored(tmp_path):
    led = Ledger(tmp_path / "s.json")
    assert led.apply_order_update(order("zzz", "buy", 1, 100.0)) == 0
    assert led.holdings() == {}


def test_reconcile_shrinks_to_account(tmp_path):
    led = Ledger(tmp_path / "s.json")
    led.add_pending(order("1", "buy", 0, None, "new"), {"symbol": "NVDA"})
    led.apply_order_update(order("1", "buy", 1.0, 100.0))
    notes = led.reconcile({"NVDA": 0.4})   # you sold some yourself
    assert led.qty("NVDA") == 0.4 and abs(led.data["holdings"]["NVDA"]["cost"] - 40) < 1e-9
    assert notes
    led.reconcile({})
    assert led.holdings() == {}
