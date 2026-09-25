from __future__ import annotations

# USD per million tokens at list price:
# input, output, cache read, 5-minute cache write, 1-hour cache write.
PRICES: dict[str, tuple[float, float, float, float, float]] = {
    "fable": (10.0, 50.0, 0.25, 12.5, 20.0),
    "opus": (4.0, 20.0, 0.20, 5.0, 8.0),
    "sonnet": (2.0, 10.0, 0.20, 2.5, 4.0),
    "haiku": (1.0, 5.0, 0.10, 1.25, 2.0),
}


def _rates(model: str | None) -> tuple[float, float, float, float, float] | None:
    name = (model or "").lower()
    return next((rates for key, rates in PRICES.items() if key in name), None)


def response_cost(model: str | None, usage: dict) -> float | None:
    rates = _rates(model)
    if rates is None:
        return None
    split = usage.get("cache_creation") or {}
    write_5m = split.get("ephemeral_5m_input_tokens")
    write_1h = split.get("ephemeral_1h_input_tokens")
    if write_5m is None and write_1h is None:
        write_1h = usage.get("cache_creation_input_tokens")
    counts = (
        usage.get("input_tokens"),
        usage.get("output_tokens"),
        usage.get("cache_read_input_tokens"),
        write_5m,
        write_1h,
    )
    return sum((count or 0) * rate for count, rate in zip(counts, rates, strict=True)) / 1e6


def estimate(model: str | None, totals: dict) -> float | None:
    rates = _rates(model)
    if rates is None:
        return None
    counts = (
        totals.get("input_tokens"),
        totals.get("output_tokens"),
        totals.get("cache_read_tokens"),
        0,
        totals.get("cache_write_tokens"),
    )
    return sum((count or 0) * rate for count, rate in zip(counts, rates, strict=True)) / 1e6
