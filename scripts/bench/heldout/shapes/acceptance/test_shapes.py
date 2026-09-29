from __future__ import annotations

import math
from pathlib import Path

import pytest


def test_shape_is_abstract() -> None:
    from shapes.base import Shape

    with pytest.raises(TypeError):
        Shape()  # type: ignore[abstract]


def test_circle() -> None:
    from shapes.base import Shape
    from shapes.circle import Circle

    circle = Circle(1)
    assert isinstance(circle, Shape)
    assert circle.name == "circle"
    assert circle.area() == pytest.approx(math.pi)
    assert circle.perimeter() == pytest.approx(2 * math.pi)
    assert circle.describe() == "circle: area=3.14, perimeter=6.28"


def test_rectangle_and_square() -> None:
    from shapes.rect import Rectangle, Square

    rect = Rectangle(2, 3)
    assert (rect.name, rect.area(), rect.perimeter()) == ("rectangle", 6, 10)
    assert rect.describe() == "rectangle: area=6.00, perimeter=10.00"
    square = Square(2)
    assert isinstance(square, Rectangle)
    assert (square.name, square.area(), square.perimeter()) == ("square", 4, 8)


@pytest.mark.parametrize("args", [("circle", 0), ("circle", -1), ("rect", 0, 1), ("rect", 1, -2)])
def test_bad_sizes_raise(args: tuple) -> None:
    from shapes.circle import Circle
    from shapes.rect import Rectangle

    kind, *values = args
    with pytest.raises(ValueError):
        (Circle if kind == "circle" else Rectangle)(*values)


def test_square_rejects_a_bad_side() -> None:
    from shapes.rect import Square

    with pytest.raises(ValueError):
        Square(0)


def test_command_writes_the_line(host: Path, run_python, tmp_path: Path) -> None:
    done = run_python(str(host / "shapes_cli.py"), "rect", "2", "3")
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "rectangle: area=6.00, perimeter=10.00"
    written = (tmp_path / "shapes.txt").read_text(encoding="utf-8")
    assert written.strip() == "rectangle: area=6.00, perimeter=10.00"


def test_command_square_in_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import shapes_cli

    monkeypatch.chdir(tmp_path)
    assert shapes_cli.main(["square", "1.5"]) == 0
    assert capsys.readouterr().out.strip() == "square: area=2.25, perimeter=6.00"


@pytest.mark.parametrize(
    "argv", [["hexagon", "1"], ["circle"], ["rect", "1"], ["circle", "abc"], ["square", "-1"]]
)
def test_command_errors(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    import shapes_cli

    monkeypatch.chdir(tmp_path)
    assert shapes_cli.main(argv) == 2
    assert capsys.readouterr().err.startswith("error: ")
