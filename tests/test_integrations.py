"""Broker/Telegram request formats, and the TradingAgents API surface we rely on."""

import inspect
import json

import pytest

from agent.broker import AlpacaBroker, BrokerError
from agent.telegram import Telegram, TelegramError


class Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._p = payload
        self.content = json.dumps(payload).encode()
        self.text = json.dumps(payload)

    def json(self):
        return self._p


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.headers = {}
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        for (m, frag), payload in self.routes.items():
            if m == method and frag in url:
                return payload if isinstance(payload, Resp) else Resp(200, payload)
        return Resp(404, {"message": "not found"})

    def post(self, url, json=None, timeout=None):
        self.calls.append(("POST", url, {"json": json}))
        method = url.rsplit("/", 1)[1]
        return Resp(200, self.routes[method](json))


def test_positions_normalise_crypto_symbols():
    s = FakeSession({("GET", "/v2/positions"): [
        {"symbol": "BTCUSD", "qty": "0.001", "market_value": "110", "current_price": "110000",
         "avg_entry_price": "100000", "asset_class": "crypto"},
        {"symbol": "NVDA", "qty": "0.5", "market_value": "90", "current_price": "180",
         "avg_entry_price": "170", "asset_class": "us_equity"}]})
    b = AlpacaBroker("k", "s", live=False, session=s)
    pos = b.positions()
    assert set(pos) == {"BTC/USD", "NVDA"} and pos["BTC/USD"].qty == 0.001
    assert s.headers["APCA-API-KEY-ID"] == "k"
    assert s.calls[0][1].startswith("https://paper-api.alpaca.markets")


def test_order_body_stock_and_crypto():
    ok = {"id": "o1", "client_order_id": "c", "symbol": "X", "side": "buy", "status": "new",
          "filled_qty": "0"}
    s = FakeSession({("POST", "/v2/orders"): ok})
    b = AlpacaBroker("k", "s", live=True, session=s)
    b.submit_limit_order("NVDA", "buy", 0.274725, 181.8, crypto=False, client_order_id="c1")
    b.submit_limit_order("BTC/USD", "sell", 0.0002, 108900.0, crypto=True, client_order_id="c2")
    stock, crypto = s.calls[0][2]["json"], s.calls[1][2]["json"]
    assert s.calls[0][1].startswith("https://api.alpaca.markets")
    assert stock == {"symbol": "NVDA", "side": "buy", "type": "limit", "qty": "0.274725",
                     "limit_price": "181.8", "time_in_force": "day", "client_order_id": "c1"}
    assert crypto["time_in_force"] == "ioc" and crypto["qty"] == "0.0002"


def test_broker_errors_are_raised():
    s = FakeSession({("GET", "/v2/account"): Resp(403, {"message": "forbidden"})})
    with pytest.raises(BrokerError, match="403"):
        AlpacaBroker("k", "s", live=False, session=s).account()
    with pytest.raises(BrokerError, match="keys missing"):
        AlpacaBroker("", "", live=True)


def test_crypto_asset_lookup_is_url_encoded():
    s = FakeSession({("GET", "/v2/assets/BTC%2FUSD"): {"tradable": True, "fractionable": True,
                                                      "min_order_size": "0.0001",
                                                      "min_trade_increment": "0.000000001",
                                                      "price_increment": "1"}})
    a = AlpacaBroker("k", "s", live=False, session=s).asset("BTC/USD")
    assert a.tradable and a.min_order_size == 0.0001


def test_telegram_approval_flow():
    updates = [[
        {"update_id": 10, "callback_query": {"id": "q1", "data": "a:p1:0",
                                             "message": {"chat": {"id": 42}}}},
        {"update_id": 11, "callback_query": {"id": "q2", "data": "a:p1:1",
                                             "message": {"chat": {"id": 999}}}},  # stranger
        {"update_id": 12, "callback_query": {"id": "q3", "data": "a:OLD:1",
                                             "message": {"chat": {"id": 42}}}},   # old plan
        {"update_id": 13, "callback_query": {"id": "q4", "data": "s:p1:1",
                                             "message": {"chat": {"id": 42}}}},
    ]]
    counter = {"n": 0}
    routes = {
        "sendMessage": lambda j: (counter.__setitem__("n", counter["n"] + 1)
                                  or {"ok": True, "result": {"message_id": counter["n"]}}),
        "getUpdates": lambda j: {"ok": True, "result": updates.pop(0) if updates else []},
        "answerCallbackQuery": lambda j: {"ok": True, "result": True},
        "editMessageReplyMarkup": lambda j: {"ok": True, "result": True},
    }
    tg = Telegram("tok", "42", session=FakeSession(routes))
    decisions, offset = tg.ask_approval("p1", ["BUY NVDA", "BUY AAPL"], "hdr", 0, 1,
                                        sleep=lambda _: None)
    assert decisions == {0: True, 1: False} and offset == 14


def test_telegram_timeout_skips_everything():
    t = {"now": 0.0}

    def clock():
        t["now"] += 30
        return t["now"]

    routes = {
        "sendMessage": lambda j: {"ok": True, "result": {"message_id": 1}},
        "getUpdates": lambda j: {"ok": True, "result": []},
        "editMessageReplyMarkup": lambda j: {"ok": True, "result": True},
    }
    tg = Telegram("tok", "42", session=FakeSession(routes))
    decisions, _ = tg.ask_approval("p", ["BUY NVDA"], "hdr", 0, 1, sleep=lambda _: None,
                                   clock=clock)
    assert decisions == {0: False}


def test_tradingagents_api_surface(make_settings):
    """The parts of TradingAgents this agent calls must exist with these shapes."""
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    from tradingagents.portfolio import PortfolioContext

    from agent.analyst import Analyst

    sig = inspect.signature(TradingAgentsGraph.propagate)
    assert {"company_name", "trade_date", "asset_type", "portfolio"} <= set(sig.parameters)
    init = inspect.signature(TradingAgentsGraph.__init__)
    assert {"selected_analysts", "config", "callbacks"} <= set(init.parameters)

    s = make_settings()
    cfg = Analyst(s)._config()
    for k in ("llm_provider", "deep_think_llm", "quick_think_llm", "max_debate_rounds",
              "max_risk_discuss_rounds", "results_dir", "data_cache_dir", "memory_log_path"):
        assert k in DEFAULT_CONFIG and k in cfg
    assert cfg["deep_think_llm"] == "gpt-6-sol" and str(s.data_dir) in cfg["results_dir"]

    ctx = PortfolioContext.model_validate(
        {"cash": 450.0, "currency": "USD",
         "positions": [{"ticker": "BTC-USD", "quantity": 0.0004, "average_price": 110000.0}]})
    assert "BTC-USD" in ctx.render("BTC-USD")


def test_graph_builds_with_our_config(make_settings, monkeypatch):
    """Construct the real graph (no network) for stocks and crypto with our settings."""
    s = make_settings()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    a = Analyst_with_tracker(s)
    g_stock = a._graph("stock")
    g_crypto = a._graph("crypto")
    assert tuple(g_stock.selected_analysts) == ("market", "social", "news", "fundamentals")
    assert tuple(g_crypto.selected_analysts) == ("market", "social", "news")


def Analyst_with_tracker(s):
    from agent.analyst import Analyst
    from agent.costs import SpendTracker
    return Analyst(s, callbacks=[SpendTracker(s.prices_per_million, s.deep_model)])


def test_telegram_stop_during_approval():
    updates = [[{"update_id": 5, "message": {"chat": {"id": 42}, "text": "/stop"}}]]
    routes = {
        "sendMessage": lambda j: {"ok": True, "result": {"message_id": 1}},
        "getUpdates": lambda j: {"ok": True, "result": updates.pop(0) if updates else []},
        "editMessageReplyMarkup": lambda j: {"ok": True, "result": True},
    }
    tg = Telegram("tok", "42", session=FakeSession(routes))
    decisions, _ = tg.ask_approval("p", ["BUY NVDA", "BUY AAPL"], "hdr", 0, 60,
                                   sleep=lambda _: None)
    assert decisions == {0: False, 1: False} and tg.stop_requested


def test_progress_lines_from_a_real_langgraph():
    """Stage names reach our graph-level callback the way TradingAgents invokes its graph."""
    from typing import TypedDict

    from langgraph.graph import END, START, StateGraph

    from agent.progress import ProgressPrinter

    class S(TypedDict):
        x: int

    g = StateGraph(S)
    g.add_node("Msg Clear Market", lambda s: {"x": s["x"] + 1})
    g.add_node("Trader", lambda s: {"x": s["x"] + 1})
    g.add_node("helper", lambda s: {"x": s["x"] + 1})
    g.add_edge(START, "Msg Clear Market")
    g.add_edge("Msg Clear Market", "helper")
    g.add_edge("helper", "Trader")
    g.add_edge("Trader", END)
    lines = []
    p = ProgressPrinter(out=lambda text, **_: lines.append(text))
    p.begin("NVDA", 1, 3)
    g.compile().invoke({"x": 0}, config={"callbacks": [p]})
    assert lines[0].startswith("[1/3] NVDA")
    assert any("market analyst done" in x for x in lines)
    assert any("trader done" in x for x in lines)
    assert not any("helper" in x for x in lines)


def test_progress_is_attached_to_tradingagents_graph(make_settings, monkeypatch):
    from agent.analyst import Analyst
    from agent.progress import ProgressPrinter

    s = make_settings()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    p = ProgressPrinter()
    g = Analyst(s, progress=p)._graph("stock")
    args = g.propagator.get_graph_args()
    assert p in args["config"]["callbacks"] and args["config"]["recursion_limit"] > 0


def test_telegram_retries_network_errors():
    import requests as rq

    class Flaky(FakeSession):
        def __init__(self):
            super().__init__({"sendMessage": lambda j: {"ok": True, "result": {"message_id": 7}}})
            self.fails = 1

        def post(self, url, json=None, timeout=None):
            if self.fails:
                self.fails -= 1
                raise rq.ReadTimeout("read timed out")
            return super().post(url, json=json, timeout=timeout)

    tg = Telegram("tok", "42", session=Flaky())
    tg._sleep = lambda _: None
    assert tg.send("hello") == 7


def test_telegram_gives_up_after_retries():
    import requests as rq

    class Dead(FakeSession):
        def post(self, url, json=None, timeout=None):
            raise rq.ConnectionError("down")

    tg = Telegram("tok", "42", session=Dead({}))
    tg._sleep = lambda _: None
    with pytest.raises(TelegramError):
        tg.send("hello")
