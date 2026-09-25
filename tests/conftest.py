import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.broker import Account, AssetInfo, OrderStatus, Position  # noqa: E402
from agent.settings import load_settings  # noqa: E402

CONFIG = """
mode: {mode}
watchlist:
  us_stocks: [NVDA, AVGO, MSFT, AAPL, ABBV]
  crypto: [BTC-USD, ETH-USD, SOL-USD]
  asx_signals: [NDQ.AX, CBA.AX, BHP.AX]
risk:
  capital_cap_usd: 500
  buy_weight: 0.10
  overweight_weight: 0.05
  min_order_usd: 5
  max_orders_per_run: 6
  limit_buffer_pct: 1.0
  max_loss_usd: 100
approval:
  require_in_paper: false
  timeout_minutes: 1
llm:
  deep_model: gpt-6-sol
  quick_model: gpt-6-luna
  daily_budget_usd: 3.0
  prices_per_million:
    gpt-6-sol: [2.0, 10.0]
    gpt-6-luna: [0.1, 0.5]
"""


@pytest.fixture
def make_settings(tmp_path, monkeypatch):
    for k in ("OPENAI_API_KEY", "ALPACA_PAPER_KEY_ID", "ALPACA_PAPER_SECRET_KEY",
              "ALPACA_LIVE_KEY_ID", "ALPACA_LIVE_SECRET_KEY", "TELEGRAM_BOT_TOKEN",
              "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(k, raising=False)

    def _make(mode="paper", telegram=False, extra_env=None):
        (tmp_path / "config.yaml").write_text(CONFIG.format(mode=mode))
        env = ["OPENAI_API_KEY=sk-test",
               "ALPACA_PAPER_KEY_ID=pk", "ALPACA_PAPER_SECRET_KEY=ps",
               "ALPACA_LIVE_KEY_ID=lk", "ALPACA_LIVE_SECRET_KEY=ls"]
        if telegram:
            env += ["TELEGRAM_BOT_TOKEN=tok", "TELEGRAM_CHAT_ID=42"]
        env += extra_env or []
        (tmp_path / ".env").write_text("\n".join(env) + "\n")
        return load_settings(project_dir=tmp_path)

    return _make


PRICES = {"NVDA": 180.0, "AVGO": 350.0, "MSFT": 510.0, "AAPL": 240.0, "ABBV": 210.0,
          "BTC/USD": 110_000.0, "ETH/USD": 4_200.0, "SOL/USD": 210.0, "SPY": 660.0}


def asset(sym, fractionable=True):
    crypto = "/" in sym
    return AssetInfo(sym, True, fractionable,
                     min_order_size=0.000_01 if crypto else 0.0,
                     min_trade_increment=0.000_000_001 if crypto else 0.0,
                     price_increment=0.01 if crypto else 0.0)


class FakeBroker:
    """In-memory Alpaca: stock orders stay open (market closed), crypto IOC fills at once."""

    def __init__(self, cash=1000.0, positions=None, trading_day=True):
        self.cash = cash
        self.pos = dict(positions or {})  # symbol -> qty
        self.orders: dict[str, OrderStatus] = {}
        self.submitted = []
        self.trading_day = trading_day
        self.n = 0

    def account(self):
        return Account(self.cash, self.cash + sum(q * PRICES[s] for s, q in self.pos.items()),
                       "ACTIVE", False, "USD")

    def positions(self):
        return {s: Position(s, q, q * PRICES[s], PRICES[s], PRICES[s],
                            "crypto" if "/" in s else "us_equity")
                for s, q in self.pos.items() if q > 0}

    def latest_price(self, symbol, crypto):
        return PRICES[symbol]

    def asset(self, symbol):
        return asset(symbol)

    def price_at(self, symbol, crypto, start_iso):
        return PRICES[symbol] / 1.05     # everything is up 5% over the week

    def is_trading_day(self, date_iso):
        return self.trading_day

    def submit_limit_order(self, symbol, side, qty, limit_price, crypto, client_order_id):
        self.n += 1
        oid = f"order-{self.n}"
        self.submitted.append((symbol, side, qty, limit_price, crypto))
        st = OrderStatus(oid, client_order_id, symbol, side, "new", 0.0, None)
        if crypto:  # IOC fills immediately at the reference price
            st = OrderStatus(oid, client_order_id, symbol, side, "filled", qty, PRICES[symbol])
            self._settle(symbol, side, qty)
        self.orders[oid] = st
        return OrderStatus(oid, client_order_id, symbol, side, "new", 0.0, None)

    def _settle(self, symbol, side, qty):
        sign = 1 if side == "buy" else -1
        self.pos[symbol] = self.pos.get(symbol, 0.0) + sign * qty
        self.cash -= sign * qty * PRICES[symbol]

    def fill_open_orders(self):
        """Simulate the next US open filling queued stock orders."""
        for oid, st in list(self.orders.items()):
            if st.status == "new":
                self._settle(st.symbol, st.side, self._qty(oid))
                self.orders[oid] = OrderStatus(oid, st.client_order_id, st.symbol, st.side,
                                               "filled", self._qty(oid), PRICES[st.symbol])

    def _qty(self, oid):
        idx = int(oid.split("-")[1]) - 1
        return self.submitted[idx][2]

    def get_order(self, oid):
        return self.orders[oid]


class FakeAnalyst:
    def __init__(self, ratings, cost_per_call=0.0, fail=()):
        self.ratings = ratings
        self.callbacks = []
        self.cost = cost_per_call
        self.fail = set(fail)
        self.calls = []

    def analyse(self, inst, trade_date, portfolio):
        from agent.analyst import Analysis
        self.calls.append((inst.ticker, portfolio))
        for cb in self.callbacks:
            cb.spent += self.cost
        if inst.ticker in self.fail:
            raise RuntimeError("data vendor down")
        r = self.ratings.get(inst.ticker, "Hold")
        return Analysis(inst.ticker, r, f"{r} because tests", f"FINAL: Rating: {r}")


class FakeTelegram:
    def __init__(self, approve=None, commands=()):
        self.approve = approve  # None = approve all, else set of indexes
        self.commands = list(commands)
        self.sent = []
        self.asked = []

    def send(self, text, buttons=None):
        self.sent.append(text)
        return len(self.sent)

    def read_commands(self, offset):
        from agent.telegram import Command
        cmds = [Command(c, i) for i, c in enumerate(self.commands)]
        self.commands = []
        return cmds, offset + len(cmds)

    def ask_approval(self, plan_id, lines, header, offset, timeout_minutes):
        self.asked.append(lines)
        return ({i: (self.approve is None or i in self.approve) for i in range(len(lines))},
                offset)
