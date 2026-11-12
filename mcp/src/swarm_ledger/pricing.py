from __future__ import annotations

import re

# USD per million tokens at list price:
# input, output, cache read, 5-minute cache write, 1-hour cache write.
# Source: https://platform.claude.com/docs/en/about-claude/pricing, read 2026-11-12.
# A session transcript records token counts per response and no cost, so these rows price
# every response; the `pricing` setting overrides them without a release.
RATE_NAMES = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")
Rates = tuple[float, float, float, float, float]

_FABLE_5_2: Rates = (8.0, 40.0, 0.20, 10.0, 16.0)
_FABLE_5_1: Rates = (10.0, 50.0, 0.25, 12.5, 20.0)
_FABLE_5: Rates = (10.0, 50.0, 1.0, 12.5, 20.0)
_OPUS_5_5: Rates = (4.0, 20.0, 0.20, 5.0, 8.0)
_OPUS_4_5_TO_5: Rates = (5.0, 25.0, 0.50, 6.25, 10.0)
_OPUS_4: Rates = (15.0, 75.0, 1.50, 18.75, 30.0)
_SONNET_5_5: Rates = (2.0, 10.0, 0.10, 2.5, 4.0)
_SONNET_5: Rates = (2.0, 10.0, 0.20, 2.5, 4.0)
_SONNET_4: Rates = (3.0, 15.0, 0.30, 3.75, 6.0)
_HAIKU_5_5: Rates = (0.10, 0.50, 0.01, 0.125, 0.20)
_HAIKU_4_5: Rates = (1.0, 5.0, 0.10, 1.25, 2.0)

# (family, major, minor): the minor is None for an id with no minor version, such as
# claude-opus-5 or claude-opus-4-20250514.
VERSION_PRICES: dict[tuple[str, int, int | None], Rates] = {
    **{(family, 5, 2): _FABLE_5_2 for family in ("fable", "mythos")},
    **{(family, 5, 1): _FABLE_5_1 for family in ("fable", "mythos")},
    **{(family, 5, None): _FABLE_5 for family in ("fable", "mythos")},
    ("opus", 5, 5): _OPUS_5_5,
    ("opus", 5, None): _OPUS_4_5_TO_5,
    **{("opus", 4, minor): _OPUS_4_5_TO_5 for minor in (5, 6, 7, 8)},
    ("opus", 4, 1): _OPUS_4,
    ("opus", 4, None): _OPUS_4,
    ("sonnet", 5, 5): _SONNET_5_5,
    ("sonnet", 5, None): _SONNET_5,
    **{("sonnet", 4, minor): _SONNET_4 for minor in (None, 5, 6)},
    ("haiku", 5, 5): _HAIKU_5_5,
    ("haiku", 4, 5): _HAIKU_4_5,
}
# Haiku 5.5 prices a request whose prompt (input, cache reads, and cache writes) is over
# 100,000 tokens at a higher row. Each request is priced on its own.
LONG_PROMPT_TOKENS = 100_000
LONG_PROMPT_PRICES: dict[tuple[str, int, int | None], Rates] = {
    ("haiku", 5, 5): (0.50, 2.50, 0.05, 0.625, 1.0),
}
# A one- or two-digit minor: a longer run of digits is a release date.
_VERSION = re.compile(r"(fable|mythos|opus|sonnet|haiku)-(\d+)(?:-(\d{1,2})(?!\d))?")


# A model named only by its family (an alias such as "sonnet", or a version with no row
# above) is priced as the family's newest model.
NEWEST: dict[str, tuple[str, int, int | None]] = {
    "fable": ("fable", 5, 2),
    "mythos": ("mythos", 5, 2),
    "opus": ("opus", 5, 5),
    "sonnet": ("sonnet", 5, 5),
    "haiku": ("haiku", 5, 5),
}
DEFAULT_PRICES: dict[str, Rates] = {
    family: VERSION_PRICES[version] for family, version in NEWEST.items()
}


PRICES: dict[str, Rates] = dict(DEFAULT_PRICES)
_OVERRIDDEN: set[str] = set()


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
    _OVERRIDDEN.clear()
    _OVERRIDDEN.update(family for family in (overrides or {}) if family in table)


def _version(model: str) -> tuple[str, int, int | None] | None:
    match = _VERSION.search(model)
    if match is None:
        return None
    minor = match.group(3)
    return match.group(1), int(match.group(2)), int(minor) if minor is not None else None


def _rates(model: str | None, prompt_tokens: int = 0) -> Rates | None:
    name = (model or "").lower()
    # A configured row first, the longest name winning, so "opus-5" beats "opus".
    for key in sorted(_OVERRIDDEN, key=len, reverse=True):
        if key in name:
            return PRICES[key]
    family = next((key for key in NEWEST if key in name), None)
    if family is None:
        return None
    version = _version(name)
    if version not in VERSION_PRICES:
        version = NEWEST[family]
    if prompt_tokens > LONG_PROMPT_TOKENS and version in LONG_PROMPT_PRICES:
        return LONG_PROMPT_PRICES[version]
    if version == NEWEST[family]:
        # The family row, which a `pricing` override of the family replaces.
        return PRICES[family]
    return VERSION_PRICES[version]


def response_cost(model: str | None, usage: dict) -> float | None:
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
    prompt = sum(count or 0 for count in (counts[0], *counts[2:]))
    rates = _rates(model, prompt)
    if rates is None:
        return None
    return sum((count or 0) * rate for count, rate in zip(counts, rates)) / 1e6


def estimate(model: str | None, totals: dict) -> float | None:
    # Totals hold no per-request prompt size, so a long-prompt tier cannot apply here.
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
