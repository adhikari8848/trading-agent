"""Print a line as each TradingAgents team member finishes, so long runs don't look stuck."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

log = logging.getLogger(__name__)

STAGES = {
    "Msg Clear Market": "market analyst",
    "Msg Clear Sentiment": "sentiment analyst",
    "Msg Clear News": "news analyst",
    "Msg Clear Fundamentals": "fundamentals analyst",
    "Bull Researcher": "bull researcher",
    "Bear Researcher": "bear researcher",
    "Research Manager": "research manager",
    "Trader": "trader",
    "Aggressive Analyst": "risk team: aggressive",
    "Neutral Analyst": "risk team: neutral",
    "Conservative Analyst": "risk team: conservative",
    "Portfolio Manager": "portfolio manager",
}


class ProgressPrinter(BaseCallbackHandler):
    """Graph-level callback: reports finished stages with elapsed time and spend."""

    def __init__(self, spend_source=None, out=print):
        super().__init__()
        self._lock = threading.Lock()
        self._runs: dict[Any, str] = {}
        self.ticker = ""
        self.started = time.monotonic()
        self.spend_source = spend_source  # object with a .spent attribute
        self.out = out

    def begin(self, ticker: str, index: int, total: int) -> None:
        self.ticker = ticker
        self.started = time.monotonic()
        self._emit(f"[{index}/{total}] {ticker}: analysing (usually 3-6 minutes)")

    def _emit(self, text: str) -> None:
        log.info(text)
        try:
            self.out(text, flush=True)
        except TypeError:
            self.out(text)

    def on_chain_start(self, serialized, inputs, *, run_id=None, **kwargs: Any) -> None:
        name = kwargs.get("name") or (serialized or {}).get("name")
        if name in STAGES:
            with self._lock:
                self._runs[run_id] = name

    def on_chain_end(self, outputs, *, run_id=None, **kwargs: Any) -> None:
        with self._lock:
            name = self._runs.pop(run_id, None)
        if name:
            secs = int(time.monotonic() - self.started)
            spent = getattr(self.spend_source, "spent", None)
            cost = f", ${spent:.2f} spent today" if spent is not None else ""
            self._emit(f"      {self.ticker} · {STAGES[name]} done ({secs // 60}m{secs % 60:02d}s{cost})")

    def on_chain_error(self, error, *, run_id=None, **kwargs: Any) -> None:
        with self._lock:
            self._runs.pop(run_id, None)
