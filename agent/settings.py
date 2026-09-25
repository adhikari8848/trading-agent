"""Load config.yaml and .env into one validated Settings object."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent.parent
RATINGS = ("Buy", "Overweight", "Hold", "Underweight", "Sell")
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


class SettingsError(ValueError):
    pass


@dataclass
class Instrument:
    """One watchlist entry."""

    ticker: str          # as TradingAgents / Yahoo know it: NVDA, BTC-USD, NDQ.AX
    kind: str            # "us_stock" | "crypto" | "asx"
    broker_symbol: str | None  # Alpaca symbol, or None for signal-only

    @property
    def tradable(self) -> bool:
        return self.broker_symbol is not None

    @property
    def asset_type(self) -> str:
        return "crypto" if self.kind == "crypto" else "stock"


@dataclass
class Settings:
    mode: str
    instruments: list[Instrument]
    capital_cap_usd: float
    buy_weight: float
    overweight_weight: float
    underweight_weight: float
    min_order_usd: float
    max_orders_per_run: int
    limit_buffer_pct: float
    max_loss_usd: float
    approval_in_paper: bool
    approval_timeout_minutes: int
    llm_provider: str
    deep_model: str
    quick_model: str
    max_debate_rounds: int
    max_risk_rounds: int
    daily_budget_usd: float
    prices_per_million: dict[str, tuple[float, float]]
    analysts: dict[str, list[str]]
    # schedule (installed with ./ta install-schedule)
    crypto_every_hours: int = 4
    stocks_time: str = "08:30"
    weekly_day: str = "Fri"
    weekly_time: str = "18:00"
    # secrets / environment
    openai_api_key: str | None = None
    alpaca_key_id: str | None = None
    alpaca_secret: str | None = None
    telegram_token: str | None = None
    telegram_chat_id: str | None = None
    project_dir: Path = field(default=PROJECT_DIR)

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    @property
    def needs_approval(self) -> bool:
        return self.is_live or self.approval_in_paper

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat_id)

    @property
    def data_dir(self) -> Path:
        return self.project_dir / "data"

    @property
    def state_path(self) -> Path:
        """Paper and live keep separate books."""
        return self.data_dir / f"state-{self.mode}.json"

    @property
    def reports_dir(self) -> Path:
        return self.project_dir / "reports"

    @property
    def logs_dir(self) -> Path:
        return self.project_dir / "logs"

    @property
    def stop_file(self) -> Path:
        return self.data_dir / "STOP"  # inside data/ so the cloud copy keeps it between runs

    def weight_for(self, rating: str) -> float | None:
        return {"Buy": self.buy_weight, "Overweight": self.overweight_weight}.get(rating)


def crypto_broker_symbol(ticker: str) -> str:
    """BTC-USD -> BTC/USD (Alpaca's crypto pair format)."""
    base, _, quote = ticker.upper().partition("-")
    if not base or quote not in ("USD", "USDT", "USDC"):
        raise SettingsError(f"crypto ticker {ticker!r} must look like BTC-USD")
    return f"{base}/{quote}"


def _instruments(watchlist: dict) -> list[Instrument]:
    out: list[Instrument] = []
    for t in watchlist.get("us_stocks") or []:
        t = str(t).upper().strip()
        if "." in t:
            raise SettingsError(f"{t} has an exchange suffix; only US tickers go under us_stocks")
        out.append(Instrument(t, "us_stock", t))
    for t in watchlist.get("crypto") or []:
        t = str(t).upper().strip()
        out.append(Instrument(t, "crypto", crypto_broker_symbol(t)))
    for t in watchlist.get("asx_signals") or []:
        t = str(t).upper().strip()
        if not t.endswith(".AX"):
            raise SettingsError(f"ASX ticker {t} should end in .AX")
        out.append(Instrument(t, "asx", None))
    seen = set()
    for i in out:
        if i.ticker in seen:
            raise SettingsError(f"{i.ticker} is listed twice in the watchlist")
        seen.add(i.ticker)
    return out


def load_settings(config_path: Path | None = None, env_path: Path | None = None,
                  project_dir: Path = PROJECT_DIR) -> Settings:
    config_path = config_path or project_dir / "config.yaml"
    env_path = env_path or project_dir / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise SettingsError(f"cannot read {config_path}: {exc}") from exc

    mode = str(raw.get("mode", "paper")).lower()
    if mode not in ("paper", "live"):
        raise SettingsError("mode must be 'paper' or 'live'")

    risk = raw.get("risk", {})
    approval = raw.get("approval", {})
    llm = raw.get("llm", {})
    analysts = raw.get("analysts", {})
    sched = raw.get("schedule", {}) or {}

    prefix = "ALPACA_LIVE" if mode == "live" else "ALPACA_PAPER"
    s = Settings(
        mode=mode,
        instruments=_instruments(raw.get("watchlist", {})),
        capital_cap_usd=float(risk.get("capital_cap_usd", 500)),
        buy_weight=float(risk.get("buy_weight", 0.10)),
        overweight_weight=float(risk.get("overweight_weight", 0.05)),
        underweight_weight=float(risk.get("underweight_weight", 0.025)),
        min_order_usd=float(risk.get("min_order_usd", 5)),
        max_orders_per_run=int(risk.get("max_orders_per_run", 6)),
        limit_buffer_pct=float(risk.get("limit_buffer_pct", 1.0)),
        max_loss_usd=float(risk.get("max_loss_usd", 100)),
        approval_in_paper=bool(approval.get("require_in_paper", False)),
        approval_timeout_minutes=int(approval.get("timeout_minutes", 180)),
        llm_provider=str(llm.get("provider", "openai")),
        deep_model=str(llm.get("deep_model", "gpt-6-sol")),
        quick_model=str(llm.get("quick_model", "gpt-6-luna")),
        max_debate_rounds=int(llm.get("max_debate_rounds", 1)),
        max_risk_rounds=int(llm.get("max_risk_rounds", 1)),
        daily_budget_usd=float(llm.get("daily_budget_usd", 3.0)),
        prices_per_million={k: (float(v[0]), float(v[1]))
                            for k, v in (llm.get("prices_per_million") or {}).items()},
        analysts={
            "stock": list(analysts.get("stock", ["market", "social", "news", "fundamentals"])),
            "crypto": list(analysts.get("crypto", ["market", "social", "news"])),
        },
        crypto_every_hours=int(sched.get("crypto_every_hours", 4)),
        stocks_time=str(sched.get("stocks_time", "08:30")),
        weekly_day=str(sched.get("weekly_day", "Fri"))[:3].title(),
        weekly_time=str(sched.get("weekly_time", "18:00")),
        openai_api_key=os.getenv("OPENAI_API_KEY") or None,
        alpaca_key_id=os.getenv(f"{prefix}_KEY_ID") or None,
        alpaca_secret=os.getenv(f"{prefix}_SECRET_KEY") or None,
        telegram_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID") or None,
        project_dir=project_dir,
    )
    _validate(s)
    return s


def _validate(s: Settings) -> None:
    if not 0 < s.capital_cap_usd <= 100_000:
        raise SettingsError("capital_cap_usd must be between 0 and 100,000")
    for name in ("buy_weight", "overweight_weight"):
        w = getattr(s, name)
        if not 0 < w <= 0.5:
            raise SettingsError(f"{name} must be between 0 and 0.5")
    if s.overweight_weight > s.buy_weight:
        raise SettingsError("overweight_weight should not exceed buy_weight")
    if not 0 <= s.underweight_weight < s.overweight_weight:
        raise SettingsError("underweight_weight must be at least 0 and below overweight_weight")
    if s.crypto_every_hours not in (1, 2, 3, 4, 6, 8, 12, 24):
        raise SettingsError("schedule.crypto_every_hours must divide 24 (1, 2, 3, 4, 6, 8, 12, 24)")
    for name in ("stocks_time", "weekly_time"):
        if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", getattr(s, name)):
            raise SettingsError(f"schedule.{name} must look like 08:30")
    if s.weekly_day not in WEEKDAYS:
        raise SettingsError("schedule.weekly_day must be Mon..Sun")
    if not 0 < s.limit_buffer_pct <= 5:
        raise SettingsError("limit_buffer_pct must be between 0 and 5")
    if s.max_orders_per_run < 1:
        raise SettingsError("max_orders_per_run must be at least 1")
    if s.max_loss_usd <= 0:
        raise SettingsError("max_loss_usd must be positive")
    valid = {"market", "social", "news", "fundamentals"}
    for kind, names in s.analysts.items():
        bad = set(names) - valid
        if bad or not names:
            raise SettingsError(f"analysts.{kind} has invalid entries: {sorted(bad) or 'empty'}")
