# PRD: tally

Python 3.10+, standard library only. Every file with code has pytest tests under `tests/`.

## Module 1: record (`tally/`)

- `tally/__init__.py`: package marker.
- `tally/record.py`: frozen dataclass `Expense` with fields `date: str`, `category: str`, and `amount: decimal.Decimal`. `parse_line(line: str) -> Expense` reads one CSV line `date,category,amount`, such as `2026-01-05,food,12.50`. It strips spaces around each field and lowercases the category. The date must be a valid `YYYY-MM-DD` date. The amount must be a decimal number greater than 0. A line with the wrong number of fields, a bad date, or a bad amount raises `ValueError`.

## Module 2: summary (`tally/summary.py`)

Depends on module 1.

- `tally/summary.py`:
  - `totals_by_category(expenses: list[Expense]) -> dict[str, Decimal]`.
  - `grand_total(expenses: list[Expense]) -> Decimal`; 0 for an empty list.
  - `format_summary(expenses: list[Expense]) -> str`: one line per category in alphabetical order, `category: total`, then a last line `total: grand total`. Every amount has exactly 2 decimals. Lines end with `\n`.

## Module 3: command (`tally_cli.py`)

Depends on modules 1 and 2.

- `tally_cli.py`: `main(argv: list[str] | None = None) -> int`. Argument: `PATH`, a CSV file with one expense per line. Blank lines are skipped. It writes `format_summary(...)` to `summary.txt` in the current folder, prints it to stdout, and returns 0. A missing or unreadable file prints `error: <reason>` to stderr and returns 2. A bad line prints `error: line N: <reason>` to stderr, where N is the 1-based line number in the file, writes no `summary.txt`, and returns 2. `python tally_cli.py` runs `main()`.
