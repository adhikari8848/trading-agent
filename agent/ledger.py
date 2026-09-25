"""The bot's own book: what it bought, what it spent, and orders still in flight.

The broker account may hold other things (anything you buy yourself). The bot
only ever sells what this ledger says it owns, so your own holdings are safe.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .broker import OrderStatus


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _stamp(when: datetime | None) -> str:
    return when.astimezone(timezone.utc).isoformat(timespec="seconds") if when else _now()


class Ledger:
    def __init__(self, path: Path):
        self.path = path
        self.data = {
            "holdings": {},          # symbol -> {"qty": float, "cost": float}
            "net_invested": 0.0,     # cash put in by buys minus cash taken out by sells
            "realized_pnl": 0.0,
            "pending_orders": {},    # order id -> details
            "trades": [],
            "spend": {},             # date -> US$ spent on the LLM
            "runs": {},              # trade date -> status
            "telegram_offset": 0,
            "halted_reason": None,
            "snapshots": [],         # value of the bot's book after each run (weekly summary)
            "ratings_log": [],       # every rating with the price at the time (call scorecard)
            "last_run": {},          # scope -> ISO time of the last completed run
        }
        if path.exists():
            self.data.update(json.loads(path.read_text(encoding="utf-8")))

    # -- persistence --------------------------------------------------------
    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, sort_keys=True)
        os.replace(tmp, self.path)

    # -- holdings -----------------------------------------------------------
    def qty(self, symbol: str) -> float:
        return float(self.data["holdings"].get(symbol, {}).get("qty", 0.0))

    def holdings(self) -> dict[str, dict]:
        return {s: h for s, h in self.data["holdings"].items() if h.get("qty", 0) > 1e-12}

    def reconcile(self, broker_qty: dict[str, float]) -> list[str]:
        """Shrink bot holdings that the account no longer fully has (manual sells, fees)."""
        notes = []
        for sym, h in list(self.data["holdings"].items()):
            have = broker_qty.get(sym, 0.0)
            if h["qty"] > have + 1e-9:
                if h["qty"] > 0:
                    h["cost"] *= max(have, 0.0) / h["qty"]
                notes.append(f"{sym}: ledger had {h['qty']:.8g}, account has {have:.8g}; adjusted")
                h["qty"] = max(have, 0.0)
            if h["qty"] <= 1e-12:
                del self.data["holdings"][sym]
        return notes

    # -- orders -------------------------------------------------------------
    def add_pending(self, order: OrderStatus, planned: dict) -> None:
        self.data["pending_orders"][order.id] = {
            **planned,
            "client_order_id": order.client_order_id,
            "recorded_filled": 0.0,
            "placed_at": _now(),
        }

    def apply_order_update(self, order: OrderStatus) -> float:
        """Book any newly filled quantity. Returns the new fill quantity."""
        p = self.data["pending_orders"].get(order.id)
        if p is None:
            return 0.0
        delta = order.filled_qty - p["recorded_filled"]
        if delta > 1e-12 and order.filled_avg_price:
            self._book_fill(order.symbol, order.side, delta, order.filled_avg_price, order.id)
            p["recorded_filled"] = order.filled_qty
        if order.status in ("filled", "canceled", "expired", "rejected", "done_for_day", "replaced"):
            del self.data["pending_orders"][order.id]
        return max(delta, 0.0)

    def _book_fill(self, symbol: str, side: str, qty: float, price: float, order_id: str) -> None:
        h = self.data["holdings"].setdefault(symbol, {"qty": 0.0, "cost": 0.0})
        notional = qty * price
        if side == "buy":
            h["qty"] += qty
            h["cost"] += notional
            self.data["net_invested"] += notional
        else:
            sell_qty = min(qty, h["qty"])
            avg = h["cost"] / h["qty"] if h["qty"] > 0 else price
            h["cost"] -= avg * sell_qty
            h["qty"] -= sell_qty
            self.data["net_invested"] -= notional
            realized = (price - avg) * sell_qty
            self.data["realized_pnl"] += realized
            if h["qty"] <= 1e-12:
                del self.data["holdings"][symbol]
        trade = {"time": _now(), "symbol": symbol, "side": side, "qty": qty,
                 "price": price, "notional": round(notional, 4), "order_id": order_id}
        if side != "buy":
            trade["realized"] = round(realized, 4)
        self.data["trades"].append(trade)

    # -- P&L, spend, runs ----------------------------------------------------
    def pnl(self, market_value: dict[str, float]) -> float:
        value = sum(market_value.get(s, 0.0) for s in self.holdings())
        return value - self.data["net_invested"]

    def add_spend(self, date: str, usd: float) -> None:
        self.data["spend"][date] = round(self.data["spend"].get(date, 0.0) + usd, 6)

    def spend_on(self, date: str) -> float:
        return float(self.data["spend"].get(date, 0.0))

    def run_status(self, trade_date: str) -> str | None:
        return self.data["runs"].get(trade_date)

    def set_run_status(self, trade_date: str, status: str) -> None:
        self.data["runs"][trade_date] = status

    # -- history for the weekly summary ---------------------------------------
    MAX_HISTORY = 3000

    def add_snapshot(self, value: float, scope: str, when: datetime | None = None) -> None:
        snaps = self.data.setdefault("snapshots", [])
        snaps.append({"time": _stamp(when), "scope": scope, "value": round(value, 4),
                      "net_invested": round(self.data["net_invested"], 4),
                      "pnl": round(value - self.data["net_invested"], 4)})
        del snaps[:-self.MAX_HISTORY]

    def log_rating(self, ticker: str, kind: str, rating: str, price: float | None,
                   when: datetime | None = None) -> None:
        log = self.data.setdefault("ratings_log", [])
        log.append({"time": _stamp(when), "ticker": ticker, "kind": kind, "rating": rating,
                    "price": price})
        del log[:-self.MAX_HISTORY]

    def mark_run(self, scope: str, when: datetime | None = None) -> None:
        self.data.setdefault("last_run", {})[scope] = _stamp(when)

    def last_run(self, scope: str) -> str | None:
        return self.data.get("last_run", {}).get(scope)

    @property
    def halted_reason(self) -> str | None:
        return self.data.get("halted_reason")

    def halt(self, reason: str) -> None:
        self.data["halted_reason"] = reason

    def clear_halt(self) -> None:
        self.data["halted_reason"] = None
