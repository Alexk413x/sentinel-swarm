# PRD: textstats

Build `textstats`, a small Python 3.10+ command-line tool that reads a text file and reports word and readability statistics. Standard library only. Every module has pytest tests under `tests/`.

## Module 1: core (`textstats/core/`)

- `textstats/__init__.py` and `textstats/core/__init__.py`: package markers. `textstats/__init__.py` defines `__version__ = "0.1.0"`.
- `textstats/core/tokenize.py`:
  - `words(text: str) -> list[str]`: lowercase words. A word is a run of letters, digits, or apostrophes inside a word (`don't` is one word). Punctuation and whitespace separate words.
  - `sentences(text: str) -> list[str]`: split on `.`, `!`, or `?` followed by whitespace or the end of the text. Strip each sentence and drop empty ones.
- `textstats/core/counts.py`:
  - `frequencies(words: list[str]) -> dict[str, int]`.
  - `top(words: list[str], n: int) -> list[tuple[str, int]]`: the `n` most frequent words, ties broken alphabetically. `n` below 1 raises `ValueError`.
- `textstats/core/readability.py`:
  - `syllables(word: str) -> int`: count vowel groups (`aeiouy`), subtract one for a silent final `e` when the word has more than one group, minimum 1.
  - `flesch_reading_ease(text: str) -> float`: `206.835 - 1.015 * (words / sentences) - 84.6 * (syllables / words)`, rounded to 1 decimal. Empty text raises `ValueError`.

## Module 2: cli (`textstats/cli/`)

Module 2 depends only on the core function names and signatures above.

- `textstats/cli/__init__.py`: package marker.
- `textstats/cli/report.py`:
  - `build_report(text: str, top_n: int) -> dict`: keys `words` (count), `sentences` (count), `unique_words`, `top` (list of `[word, count]`), `flesch_reading_ease`.
  - `format_text(report: dict) -> str`: a human-readable block, one statistic per line, then the top words as `word: count` lines.
  - `format_json(report: dict) -> str`: JSON with sorted keys and 2-space indent.
- `textstats/cli/main.py`: `main(argv: list[str] | None = None) -> int`. Arguments: `path`, `--top N` (default 10), `--json`. Prints the report to stdout and returns 0. A missing or unreadable file prints `error: <reason>` to stderr and returns 2. Empty text prints `error: no text` to stderr and returns 2.
- `textstats/__main__.py`: runs `main()` and exits with its return code, so `python -m textstats FILE` works.

## Docs

- Replace `README.md` with a short description of the tool and one usage example for each output format.
