"""Run the TradingAgents multi-agent analysis for one instrument."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from .settings import Instrument, Settings

log = logging.getLogger(__name__)


@dataclass
class Analysis:
    ticker: str
    rating: str          # Buy / Overweight / Hold / Underweight / Sell / REVIEW / ERROR
    summary: str         # short plain-text reason
    full_decision: str   # the Portfolio Manager's full write-up
    error: str | None = None


def _plain(text: str, limit: int) -> str:
    text = re.sub(r"[*_#`>|]+", "", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"


class Analyst:
    """Keeps one TradingAgentsGraph per asset type (stock / crypto)."""

    def __init__(self, settings: Settings, callbacks: list | None = None, progress=None):
        self.settings = settings
        self.callbacks = callbacks or []   # LLM-level: spend tracking
        self.progress = progress           # graph-level: stage-by-stage progress lines
        self._graphs: dict[str, object] = {}

    def _config(self) -> dict:
        from tradingagents.default_config import DEFAULT_CONFIG

        s = self.settings
        cfg = DEFAULT_CONFIG.copy()
        cfg["data_vendors"] = dict(DEFAULT_CONFIG["data_vendors"])
        cfg["llm_provider"] = s.llm_provider
        cfg["deep_think_llm"] = s.deep_model
        cfg["quick_think_llm"] = s.quick_model
        cfg["max_debate_rounds"] = s.max_debate_rounds
        cfg["max_risk_discuss_rounds"] = s.max_risk_rounds
        cfg["llm_max_retries"] = 4
        # Keep TradingAgents' own files inside this project
        data = s.data_dir / "tradingagents"
        cfg["results_dir"] = str(data / "logs")
        cfg["data_cache_dir"] = str(data / "cache")
        cfg["memory_log_path"] = str(data / "memory" / "trading_memory.md")
        return cfg

    def _graph(self, asset_type: str):
        if asset_type not in self._graphs:
            from tradingagents.graph.trading_graph import TradingAgentsGraph

            analysts = self.settings.analysts["crypto" if asset_type == "crypto" else "stock"]
            self._graphs[asset_type] = TradingAgentsGraph(
                selected_analysts=tuple(analysts),
                debug=False,
                config=self._config(),
                callbacks=self.callbacks,
            )
            if self.progress is not None:
                self._attach_progress(self._graphs[asset_type])
        return self._graphs[asset_type]

    def _attach_progress(self, graph) -> None:
        """TradingAgents only forwards graph-level callbacks when asked, so add ours."""
        prop = getattr(graph, "propagator", None)
        original = getattr(prop, "get_graph_args", None)
        if original is None:
            return
        progress = self.progress

        def get_graph_args(callbacks=None):
            return original(callbacks=[*(callbacks or []), progress])

        prop.get_graph_args = get_graph_args

    def analyse(self, inst: Instrument, trade_date: str, portfolio: dict | None) -> Analysis:
        from tradingagents.portfolio import PortfolioContext

        ctx = PortfolioContext.model_validate(portfolio) if portfolio is not None else None
        graph = self._graph(inst.asset_type)
        log.info("analysing %s (%s) for %s", inst.ticker, inst.asset_type, trade_date)
        state, signal = graph.propagate(inst.ticker, trade_date,
                                        asset_type=inst.asset_type, portfolio=ctx)
        decision = state.get("final_trade_decision", "") or ""
        rating = signal if signal in ("Buy", "Overweight", "Hold", "Underweight", "Sell") \
            else "REVIEW"
        return Analysis(inst.ticker, rating, _plain(decision, 280), decision)
