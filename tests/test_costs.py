import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from agent.costs import BudgetExceeded, SpendTracker

PRICES = {"gpt-6-sol": (2.0, 10.0), "gpt-6-luna": (0.1, 0.5)}


def result(model, tin, tout):
    msg = AIMessage(content="x", usage_metadata={"input_tokens": tin, "output_tokens": tout,
                                                 "total_tokens": tin + tout},
                    response_metadata={"model_name": model})
    return LLMResult(generations=[[ChatGeneration(message=msg)]])


def test_prices_by_model():
    t = SpendTracker(PRICES, "gpt-6-sol")
    t.on_llm_end(result("gpt-6-luna-2026-09-01", 1_000_000, 100_000))
    assert abs(t.spent - 0.15) < 1e-9
    t.on_llm_end(result("gpt-6-sol", 100_000, 10_000))
    assert abs(t.spent - (0.15 + 0.2 + 0.1)) < 1e-9


def test_unknown_model_charged_at_expensive_rate():
    t = SpendTracker(PRICES, "gpt-6-sol")
    t.on_llm_end(result("mystery", 1_000_000, 0))
    assert t.spent == 2.0


def test_hard_limit_blocks_next_call():
    t = SpendTracker(PRICES, "gpt-6-sol", already_spent=4.0, hard_limit=4.5)
    t.on_chat_model_start({}, [[]])
    t.on_llm_end(result("gpt-6-sol", 300_000, 0))
    with pytest.raises(BudgetExceeded):
        t.on_chat_model_start({}, [[]])
