"""Thin Alpaca REST client: only the calls this agent needs.

Docs: https://docs.alpaca.markets/reference
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import quote

import requests

log = logging.getLogger(__name__)

PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"

# Order states that will not change any more
FINAL_STATES = {"filled", "canceled", "expired", "rejected", "done_for_day", "replaced"}


class BrokerError(RuntimeError):
    pass


@dataclass
class Account:
    cash: float
    equity: float
    status: str
    trading_blocked: bool
    currency: str


@dataclass
class Position:
    symbol: str        # normalised: NVDA, BTC/USD
    qty: float
    market_value: float
    current_price: float
    avg_entry_price: float
    asset_class: str


@dataclass
class AssetInfo:
    symbol: str
    tradable: bool
    fractionable: bool
    min_order_size: float
    min_trade_increment: float
    price_increment: float


@dataclass
class OrderStatus:
    id: str
    client_order_id: str
    symbol: str
    side: str
    status: str
    filled_qty: float
    filled_avg_price: float | None


def normalise_symbol(symbol: str, asset_class: str) -> str:
    """Positions report crypto as BTCUSD; orders use BTC/USD. Always use the slash form."""
    if asset_class == "crypto" and "/" not in symbol:
        for quote_ccy in ("USDT", "USDC", "USD"):
            if symbol.endswith(quote_ccy):
                return f"{symbol[:-len(quote_ccy)]}/{quote_ccy}"
    return symbol


def _f(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class AlpacaBroker:
    def __init__(self, key_id: str, secret: str, live: bool,
                 session: requests.Session | None = None, timeout: float = 20):
        if not key_id or not secret:
            mode = "LIVE" if live else "PAPER"
            raise BrokerError(f"Alpaca {mode.lower()} keys missing: set ALPACA_{mode}_KEY_ID "
                              f"and ALPACA_{mode}_SECRET_KEY in .env")
        self.live = live
        self.base = LIVE_URL if live else PAPER_URL
        self.timeout = timeout
        self.http = session or requests.Session()
        self.http.headers.update({
            "APCA-API-KEY-ID": key_id,
            "APCA-API-SECRET-KEY": secret,
            "Accept": "application/json",
        })

    # -- plumbing ---------------------------------------------------------
    def _req(self, method: str, url: str, **kw):
        try:
            r = self.http.request(method, url, timeout=self.timeout, **kw)
        except requests.RequestException as exc:
            raise BrokerError(f"{method} {url} failed: {exc}") from exc
        if r.status_code >= 400:
            raise BrokerError(f"{method} {url} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else None

    def _get(self, path: str, **params):
        return self._req("GET", self.base + path, params=params or None)

    # -- account ----------------------------------------------------------
    def account(self) -> Account:
        a = self._get("/v2/account")
        return Account(
            cash=_f(a.get("cash")),
            equity=_f(a.get("equity")),
            status=str(a.get("status")),
            trading_blocked=bool(a.get("trading_blocked") or a.get("account_blocked")),
            currency=str(a.get("currency", "USD")),
        )

    def positions(self) -> dict[str, Position]:
        out = {}
        for p in self._get("/v2/positions") or []:
            cls = p.get("asset_class", "us_equity")
            sym = normalise_symbol(p["symbol"], cls)
            out[sym] = Position(
                symbol=sym,
                qty=_f(p.get("qty")),
                market_value=_f(p.get("market_value")),
                current_price=_f(p.get("current_price")),
                avg_entry_price=_f(p.get("avg_entry_price")),
                asset_class=cls,
            )
        return out

    def asset(self, symbol: str) -> AssetInfo:
        a = self._get("/v2/assets/" + quote(symbol, safe=""))
        return AssetInfo(
            symbol=symbol,
            tradable=bool(a.get("tradable")),
            fractionable=bool(a.get("fractionable")),
            min_order_size=_f(a.get("min_order_size"), 0.0),
            min_trade_increment=_f(a.get("min_trade_increment"), 0.0),
            price_increment=_f(a.get("price_increment"), 0.0),
        )

    def is_trading_day(self, date_iso: str) -> bool:
        days = self._get("/v2/calendar", start=date_iso, end=date_iso) or []
        return any(d.get("date") == date_iso for d in days)

    # -- prices -----------------------------------------------------------
    def latest_price(self, symbol: str, crypto: bool) -> float:
        if crypto:
            d = self._req("GET", f"{DATA_URL}/v1beta3/crypto/us/latest/trades",
                          params={"symbols": symbol})
            trade = (d or {}).get("trades", {}).get(symbol)
        else:
            d = self._req("GET", f"{DATA_URL}/v2/stocks/{quote(symbol, safe='')}/trades/latest",
                          params={"feed": "iex"})
            trade = (d or {}).get("trade")
        price = _f((trade or {}).get("p"))
        if price <= 0:
            raise BrokerError(f"no latest price for {symbol}")
        return price

    def price_at(self, symbol: str, crypto: bool, start_iso: str) -> float:
        """Opening price of the first bar at or after start_iso (for weekly comparisons)."""
        if crypto:
            d = self._req("GET", f"{DATA_URL}/v1beta3/crypto/us/bars",
                          params={"symbols": symbol, "timeframe": "1Hour",
                                  "start": start_iso, "limit": 1})
            bars = (d or {}).get("bars", {}).get(symbol) or []
        else:
            d = self._req("GET", f"{DATA_URL}/v2/stocks/{quote(symbol, safe='')}/bars",
                          params={"timeframe": "1Day", "start": start_iso[:10], "limit": 1,
                                  "feed": "iex", "adjustment": "all"})
            bars = (d or {}).get("bars") or []
        price = _f(bars[0].get("o")) if bars else 0.0
        if price <= 0:
            raise BrokerError(f"no bars for {symbol} since {start_iso}")
        return price

    # -- orders -----------------------------------------------------------
    def submit_limit_order(self, symbol: str, side: str, qty: float, limit_price: float,
                           crypto: bool, client_order_id: str) -> OrderStatus:
        body = {
            "symbol": symbol,
            "side": side,
            "type": "limit",
            "qty": _fmt(qty, 9),
            "limit_price": _fmt(limit_price, 9 if crypto else (4 if limit_price < 1 else 2)),
            # Stocks: day order (queued for the next session if the market is closed).
            # Crypto: immediate-or-cancel so nothing is left sitting on the book.
            "time_in_force": "ioc" if crypto else "day",
            "client_order_id": client_order_id,
        }
        log.info("submitting order %s", body)
        return _order(self._req("POST", self.base + "/v2/orders", json=body))

    def get_order(self, order_id: str) -> OrderStatus:
        return _order(self._get(f"/v2/orders/{order_id}"))

    def open_orders(self) -> list[OrderStatus]:
        return [_order(o) for o in self._get("/v2/orders", status="open", limit=500) or []]

    def cancel_order(self, order_id: str) -> None:
        self._req("DELETE", f"{self.base}/v2/orders/{order_id}")


def _order(o: dict) -> OrderStatus:
    return OrderStatus(
        id=o["id"],
        client_order_id=o.get("client_order_id", ""),
        symbol=normalise_symbol(o.get("symbol", ""), o.get("asset_class", "us_equity")),
        side=o.get("side", ""),
        status=o.get("status", ""),
        filled_qty=_f(o.get("filled_qty")),
        filled_avg_price=_f(o.get("filled_avg_price")) if o.get("filled_avg_price") else None,
    )


def _fmt(x: float, decimals: int) -> str:
    s = f"{x:.{decimals}f}".rstrip("0").rstrip(".")
    return s or "0"
