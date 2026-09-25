"""Turn ratings into a small list of orders, inside the risk limits.

Rules
- Buy        -> build the position up to buy_weight x capital cap
- Overweight -> build the position up to overweight_weight x capital cap
- Hold       -> do nothing
- Underweight-> trim the position down to underweight_weight x cap (2.5% = $12.50)
- Sell       -> sell everything the bot holds
- REVIEW     -> do nothing (the analysis had no readable rating)

Buys and Overweights only ever add; they never sell. The bot never shorts,
never borrows, never sells shares it did not buy itself, and never lets the
value of its holdings go above the capital cap.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .broker import AssetInfo
from .fmt import price as fmt_price
from .settings import Instrument, Settings

MIN_BROKER_NOTIONAL = 1.0  # Alpaca rejects orders below about US$1


@dataclass
class ProposedOrder:
    ticker: str
    symbol: str
    side: str
    qty: float
    limit_price: float
    est_notional: float
    rating: str
    crypto: bool
    note: str = ""

    def describe(self) -> str:
        unit = "" if self.crypto else " sh"
        verb = "BUY" if self.side == "buy" else "SELL"
        return (f"{verb} {self.symbol} ~${self.est_notional:,.2f} "
                f"({self.qty:.6g}{unit} @ limit {fmt_price(self.limit_price)}) - {self.rating}")


@dataclass
class Plan:
    orders: list[ProposedOrder] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)   # human-readable reasons
    holdings_value: float = 0.0
    room: float = 0.0
    cash: float = 0.0


def _floor(x: float, step: float) -> float:
    if step <= 0:
        return x
    return round(math.floor(x / step + 1e-9) * step, 10)


def _round_price(price: float, crypto: bool, increment: float, up: bool) -> float:
    if crypto:
        step = increment if increment > 0 else (0.01 if price >= 1 else 0.000001)
    else:
        step = 0.01 if price >= 1 else 0.0001
    n = price / step
    n = math.ceil(n - 1e-9) if up else math.floor(n + 1e-9)
    return round(n * step, 10)


def _qty_step(asset: AssetInfo, crypto: bool) -> float:
    if crypto:
        return asset.min_trade_increment if asset.min_trade_increment > 0 else 1e-6
    return 1e-6 if asset.fractionable else 1.0


def build_plan(
    settings: Settings,
    ratings: dict[str, str],                 # ticker -> rating
    instruments: list[Instrument],
    bot_qty: dict[str, float],               # broker symbol -> qty the bot owns
    prices: dict[str, float],                # broker symbol -> latest price
    assets: dict[str, AssetInfo],            # broker symbol -> asset info
    cash: float,
    busy_symbols: set[str] | None = None,    # symbols with an order still open
    halted: bool = False,
    pending_buys_usd: float = 0.0,           # buy orders placed but not yet filled
) -> Plan:
    busy_symbols = busy_symbols or set()
    buf = settings.limit_buffer_pct / 100
    cap = settings.capital_cap_usd

    holdings_value = sum(q * prices.get(s, 0.0) for s, q in bot_qty.items() if q > 0)
    room = max(0.0, cap - holdings_value - pending_buys_usd)
    cash_left = max(0.0, cash - pending_buys_usd)
    plan = Plan(holdings_value=holdings_value, room=room, cash=cash_left)

    sells: list[ProposedOrder] = []
    buys: list[tuple[int, float, ProposedOrder]] = []

    for inst in instruments:
        if not inst.tradable:
            continue
        rating = ratings.get(inst.ticker)
        if rating is None:
            continue
        sym = inst.broker_symbol
        crypto = inst.kind == "crypto"
        price = prices.get(sym)
        asset = assets.get(sym)
        held = bot_qty.get(sym, 0.0)

        if rating in ("Hold", "REVIEW"):
            continue
        if sym in busy_symbols:
            plan.skipped.append(f"{sym}: an earlier order is still open")
            continue
        if not price or price <= 0 or asset is None:
            plan.skipped.append(f"{sym}: no price or asset data")
            continue
        if not asset.tradable:
            plan.skipped.append(f"{sym}: not tradable on Alpaca")
            continue
        step = _qty_step(asset, crypto)

        if rating in ("Sell", "Underweight"):
            if held <= 0:
                continue
            if rating == "Sell":
                qty = held
            else:  # trim down to the small underweight target; repeat runs don't keep halving
                keep_value = settings.underweight_weight * cap
                excess = held * price - keep_value
                if excess < settings.min_order_usd:
                    continue
                qty = excess / price
            qty = _floor(qty, step)
            if rating == "Sell" and qty < held and not crypto and asset.fractionable:
                qty = held  # close fully; fractional positions can be sold exactly
            limit = _round_price(price * (1 - buf), crypto, asset.price_increment, up=True)
            notional = qty * limit
            if qty <= 0 or notional < MIN_BROKER_NOTIONAL or qty < asset.min_order_size:
                plan.skipped.append(f"{sym}: {rating} but position too small to sell")
                continue
            sells.append(ProposedOrder(inst.ticker, sym, "sell", qty, limit, notional,
                                       rating, crypto,
                                       "close position" if rating == "Sell" else
                                       f"trim to ${settings.underweight_weight * cap:,.2f}"))
            continue

        weight = settings.weight_for(rating)
        if weight is None:
            continue
        if halted:
            plan.skipped.append(f"{sym}: {rating} ignored, buying is halted")
            continue
        target = weight * cap
        want = target - held * price
        if want < settings.min_order_usd:
            continue  # already at or near target
        limit = _round_price(price * (1 + buf), crypto, asset.price_increment, up=False)
        amount = min(want, room, cash_left)
        if amount < settings.min_order_usd:
            reason = "capital cap reached" if room < cash_left else "not enough cash"
            plan.skipped.append(f"{sym}: {rating} but {reason}")
            continue
        qty = _floor(amount / limit, step)
        notional = qty * limit
        if qty <= 0 or notional < max(settings.min_order_usd, MIN_BROKER_NOTIONAL) \
                or qty < asset.min_order_size:
            plan.skipped.append(f"{sym}: {rating} but order would be below the minimum size")
            continue
        room -= notional
        cash_left -= notional
        priority = 0 if rating == "Buy" else 1
        buys.append((priority, -notional, ProposedOrder(
            inst.ticker, sym, "buy", qty, limit, notional, rating, crypto,
            f"target ${target:,.0f}")))

    buys.sort(key=lambda t: (t[0], t[1]))
    ordered = sells + [b[2] for b in buys]
    if len(ordered) > settings.max_orders_per_run:
        for o in ordered[settings.max_orders_per_run:]:
            plan.skipped.append(f"{o.symbol}: over the {settings.max_orders_per_run}-order limit")
        ordered = ordered[:settings.max_orders_per_run]
    plan.orders = ordered
    return plan
