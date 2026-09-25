"""Track OpenAI spend during a run and stop it if it runs away."""

from __future__ import annotations

import threading
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult


class BudgetExceeded(RuntimeError):
    pass


class SpendTracker(BaseCallbackHandler):
    """Adds up token cost per call. Raises BudgetExceeded before a new call
    once the hard limit is passed, so a stuck run cannot burn money."""

    raise_error = True  # let BudgetExceeded propagate out of LangChain

    def __init__(self, prices: dict[str, tuple[float, float]], fallback_model: str,
                 already_spent: float = 0.0, hard_limit: float | None = None):
        super().__init__()
        self._lock = threading.Lock()
        self.prices = prices
        self.fallback = prices.get(fallback_model, (2.0, 10.0))
        self.spent = already_spent
        self.hard_limit = hard_limit
        self.tokens_in = 0
        self.tokens_out = 0
        self.calls = 0

    def _price_for(self, model: str | None) -> tuple[float, float]:
        if model:
            for name, price in self.prices.items():
                if model == name or model.startswith(name):
                    return price
        return self.fallback  # unknown model: assume the expensive one

    def on_chat_model_start(self, serialized: dict[str, Any], messages, **kwargs: Any) -> None:
        self._check()

    def on_llm_start(self, serialized: dict[str, Any], prompts, **kwargs: Any) -> None:
        self._check()

    def _check(self) -> None:
        with self._lock:
            if self.hard_limit is not None and self.spent >= self.hard_limit:
                raise BudgetExceeded(
                    f"OpenAI spend ${self.spent:.2f} reached the hard limit ${self.hard_limit:.2f}")
            self.calls += 1

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        tin = tout = 0
        model = None
        for gens in response.generations or []:
            for g in gens:
                msg = getattr(g, "message", None)
                usage = getattr(msg, "usage_metadata", None) or {}
                tin += int(usage.get("input_tokens", 0) or 0)
                tout += int(usage.get("output_tokens", 0) or 0)
                meta = getattr(msg, "response_metadata", None) or {}
                model = model or meta.get("model_name") or meta.get("model")
        if not (tin or tout):
            usage = (response.llm_output or {}).get("token_usage") or {}
            tin = int(usage.get("prompt_tokens", 0) or 0)
            tout = int(usage.get("completion_tokens", 0) or 0)
        model = model or (response.llm_output or {}).get("model_name")
        pin, pout = self._price_for(model)
        with self._lock:
            self.tokens_in += tin
            self.tokens_out += tout
            self.spent += tin / 1e6 * pin + tout / 1e6 * pout
