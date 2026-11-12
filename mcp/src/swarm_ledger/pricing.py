from __future__ import annotations

# USD per million tokens at list price:
# input, output, cache read, 5-minute cache write, 1-hour cache write.
RATE_NAMES = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")
Rates = tuple[float, float, float, float, float]
DEFAULT_PRICES: dict[str, Rates] = {
    "fable": (10.0, 50.0, 0.25, 12.5, 20.0),
    "opus": (4.0, 20.0, 0.20, 5.0, 8.0),
    "sonnet": (2.0, 10.0, 0.20, 2.5, 4.0),
    "haiku": (1.0, 5.0, 0.10, 1.25, 2.0),
}


PRICES: dict[str, Rates] = dict(DEFAULT_PRICES)


def normalize_overrides(raw: object) -> dict[str, dict[str, float]]:
    """The settings file's `pricing:` mapping, family -> {rate name: USD per million tokens}.

    A family that is not a mapping, or a rate that is not a non-negative number, is dropped.
    """
    if not isinstance(raw, dict):
        return {}
    found: dict[str, dict[str, float]] = {}
    for family, rates in raw.items():
        if not isinstance(rates, dict) or not str(family).strip():
            continue
        clean: dict[str, float] = {}
        for name in RATE_NAMES:
            value = rates.get(name)
            if value is None or isinstance(value, bool):
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if number >= 0:
                clean[name] = number
        if clean:
            found[str(family).strip().lower()] = clean
    return found


def configure(overrides: dict[str, dict[str, float]] | None) -> None:
    """Replace the price table: the built-in rows, with each override merged onto its family.

    A family with no built-in row needs all five rates; a partial one is ignored.
    """
    table: dict[str, Rates] = dict(DEFAULT_PRICES)
    for family, given in (overrides or {}).items():
        base = table.get(family)
        merged: dict[str, float] = dict(zip(RATE_NAMES, base)) if base is not None else {}
        merged.update(given)
        if all(name in merged for name in RATE_NAMES):
            table[family] = (
                merged["input"],
                merged["output"],
                merged["cache_read"],
                merged["cache_write_5m"],
                merged["cache_write_1h"],
            )
    PRICES.clear()
    PRICES.update(table)


def _rates(model: str | None) -> Rates | None:
    name = (model or "").lower()
    # The longest family name first, so a configured "opus-5" beats "opus".
    for key in sorted(PRICES, key=len, reverse=True):
        if key in name:
            return PRICES[key]
    return None


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
    return sum((count or 0) * rate for count, rate in zip(counts, rates)) / 1e6


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
    return sum((count or 0) * rate for count, rate in zip(counts, rates)) / 1e6
