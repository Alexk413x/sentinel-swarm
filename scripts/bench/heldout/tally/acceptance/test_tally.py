from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

CSV = "2026-01-05, Food ,12.50\n\n2026-01-06,rent,800\n2026-01-07,food,7.25\n"
SUMMARY = "food: 19.75\nrent: 800.00\ntotal: 819.75\n"


def test_parse_line() -> None:
    from tally.record import Expense, parse_line

    expense = parse_line("2026-01-05, Food ,12.50")
    assert expense == Expense("2026-01-05", "food", Decimal("12.50"))
    with pytest.raises(AttributeError):
        expense.category = "x"  # type: ignore[misc]


@pytest.mark.parametrize(
    "line",
    [
        "2026-01-05,food",
        "2026-01-05,food,1,2",
        "2026-02-30,food,1",
        "05/01/2026,food,1",
        "2026-01-05,food,abc",
        "2026-01-05,food,0",
        "2026-01-05,food,-3",
    ],
)
def test_parse_line_rejects(line: str) -> None:
    from tally.record import parse_line

    with pytest.raises(ValueError):
        parse_line(line)


def test_summary_functions() -> None:
    from tally.record import parse_line
    from tally.summary import format_summary, grand_total, totals_by_category

    expenses = [parse_line(line) for line in CSV.splitlines() if line.strip()]
    assert totals_by_category(expenses) == {"food": Decimal("19.75"), "rent": Decimal("800")}
    assert grand_total(expenses) == Decimal("819.75")
    assert grand_total([]) == 0
    assert format_summary(expenses) == SUMMARY


def test_command(host: Path, run_python, tmp_path: Path) -> None:
    source = tmp_path / "expenses.csv"
    source.write_text(CSV, encoding="utf-8")
    done = run_python(str(host / "tally_cli.py"), str(source))
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == SUMMARY.strip()
    assert (tmp_path / "summary.txt").read_text(encoding="utf-8") == SUMMARY


def test_command_bad_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import tally_cli

    source = tmp_path / "bad.csv"
    source.write_text("2026-01-05,food,1\n\n2026-01-06,rent,oops\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert tally_cli.main([str(source)]) == 2
    assert capsys.readouterr().err.startswith("error: line 3: ")
    assert not (tmp_path / "summary.txt").exists()


def test_command_missing_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import tally_cli

    monkeypatch.chdir(tmp_path)
    assert tally_cli.main([str(tmp_path / "nope.csv")]) == 2
    assert capsys.readouterr().err.startswith("error: ")
