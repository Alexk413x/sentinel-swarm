from __future__ import annotations

from pathlib import Path

import pytest


def test_factors() -> None:
    from units.length import FACTORS

    assert FACTORS == {
        "mm": 0.001,
        "cm": 0.01,
        "m": 1.0,
        "km": 1000.0,
        "in": 0.0254,
        "ft": 0.3048,
        "mi": 1609.344,
    }


def test_conversions() -> None:
    from units.length import convert, from_meters, to_meters

    assert to_meters(2, "km") == pytest.approx(2000)
    assert from_meters(0.3048, "ft") == pytest.approx(1)
    assert convert(1, "mi", "km") == pytest.approx(1.609344)
    assert convert(12, "in", "ft") == pytest.approx(1)


def test_unknown_unit_is_named() -> None:
    from units.length import convert

    with pytest.raises(ValueError, match="furlong"):
        convert(1, "furlong", "m")
    with pytest.raises(ValueError):
        convert(1, "KM", "m")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("12.5 km", (12.5, "km")),
        ("3km", (3.0, "km")),
        (" 7 ft ", (7.0, "ft")),
        ("-2 m", (-2.0, "m")),
    ],
)
def test_parse_quantity(text: str, expected: tuple[float, str]) -> None:
    from units.parse import parse_quantity

    value, unit = parse_quantity(text)
    assert (value, unit) == (pytest.approx(expected[0]), expected[1])


@pytest.mark.parametrize("text", ["", "km", "12", "5 parsecs", "abc m"])
def test_parse_rejects(text: str) -> None:
    from units.parse import parse_quantity

    with pytest.raises(ValueError):
        parse_quantity(text)


def test_command(host: Path, run_python) -> None:
    done = run_python(str(host / "convert_cli.py"), "5 km", "mi")
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "3.1069 mi"


def test_command_in_process(capsys: pytest.CaptureFixture[str]) -> None:
    import convert_cli

    assert convert_cli.main(["1 ft", "in"]) == 0
    assert capsys.readouterr().out.strip() == "12.0000 in"


@pytest.mark.parametrize("argv", [["5 km", "parsec"], ["five km", "mi"]])
def test_command_errors(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    import convert_cli

    assert convert_cli.main(argv) == 2
    assert capsys.readouterr().err.startswith("error: ")
