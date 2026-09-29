# PRD: units

Python 3.10+, standard library only. Every file with code has pytest tests under `tests/`.

## Module 1: length (`units/`)

- `units/__init__.py`: package marker.
- `units/length.py`:
  - `FACTORS: dict[str, float]`, meters per unit: `mm` 0.001, `cm` 0.01, `m` 1.0, `km` 1000.0, `in` 0.0254, `ft` 0.3048, `mi` 1609.344.
  - `to_meters(value: float, unit: str) -> float` and `from_meters(value: float, unit: str) -> float`.
  - `convert(value: float, from_unit: str, to_unit: str) -> float`.
  - Unit names are case-sensitive. An unknown unit raises `ValueError` whose message names the unit.

## Module 2: parse (`units/parse.py`)

Depends on module 1.

- `units/parse.py`: `parse_quantity(text: str) -> tuple[float, str]` reads a number followed by a unit, with or without spaces between them and around them: `"12.5 km"`, `"3km"`, and `" 7 ft "` all parse. A negative number parses. Text with no number, no unit, or a unit `FACTORS` does not list raises `ValueError`.

## Module 3: command (`convert_cli.py`)

Depends on modules 1 and 2.

- `convert_cli.py`: `main(argv: list[str] | None = None) -> int`. Arguments: `QUANTITY` (such as `"5 km"`) and `TO_UNIT`. It prints the converted value with 4 decimals and the unit, such as `3.1069 mi`, to stdout and returns 0. A quantity or unit it cannot read prints `error: <reason>` to stderr and returns 2. `python convert_cli.py` runs `main()`.
