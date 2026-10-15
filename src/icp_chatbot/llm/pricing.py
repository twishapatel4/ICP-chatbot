"""Token prices and cost accounting, from the verified price pages (cost PDF, 2026-10-15).

Prices are USD per 1M tokens for prompts up to 200k tokens on the global endpoint. Thinking
tokens are billed as output. Regional (non-global) endpoints cost 10% more for Gemini 3+
models; that premium is not applied here because the project uses the global endpoint.
"""

from dataclasses import dataclass
from datetime import date

from icp_chatbot.llm.types import Usage


class UnknownModelError(KeyError):
    pass


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cached_input: float


_EPOCH = date(2000, 1, 1)

# model -> [(effective_from, price)] in ascending order of date.
PRICES: dict[str, list[tuple[date, Price]]] = {
    "gemini-3.1-flash-lite": [(_EPOCH, Price(0.25, 1.50, 0.025))],
    # Introductory price through 2026-12-31, standard price from 2027-01-01.
    "gemini-3.8-flash": [
        (_EPOCH, Price(0.75, 3.75, 0.075)),
        (date(2027, 1, 1), Price(1.50, 7.50, 0.15)),
    ],
    # The cached-input price for 3.5 Flash was not confirmed; 0.15 is an assumption.
    "gemini-3.5-flash": [(_EPOCH, Price(1.50, 9.00, 0.15))],
    "gemini-3.5-flash-lite": [(_EPOCH, Price(0.30, 2.50, 0.03))],
    "gemini-3.1-pro-preview": [(_EPOCH, Price(2.00, 12.00, 0.20))],
    "gemini-2.5-pro": [(_EPOCH, Price(1.25, 10.00, 0.125))],
    "gemini-2.5-flash": [(_EPOCH, Price(0.30, 2.50, 0.03))],
}


def price_for(model: str, on: date | None = None) -> Price:
    on = on or date.today()
    key = model.removeprefix("models/")
    if key not in PRICES:
        raise UnknownModelError(key)
    current = None
    for effective_from, price in PRICES[key]:
        if effective_from <= on:
            current = price
    assert current is not None
    return current


def cost_usd(model: str, usage: Usage, on: date | None = None) -> float:
    price = price_for(model, on)
    uncached_input = max(usage.input_tokens - usage.cached_tokens, 0)
    billed_output = usage.output_tokens + usage.thought_tokens
    total = (
        uncached_input * price.input
        + usage.cached_tokens * price.cached_input
        + billed_output * price.output
    )
    return total / 1_000_000
