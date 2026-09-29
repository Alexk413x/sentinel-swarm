# PRD: shapes

Python 3.10+, standard library only. Every file with code has pytest tests under `tests/`.

## Module 1: base (`shapes/`)

- `shapes/__init__.py`: package marker.
- `shapes/base.py`: abstract class `Shape` (`abc.ABC`) with abstract methods `area(self) -> float` and `perimeter(self) -> float`, an abstract property `name` (a `str`), and a method `describe(self) -> str` that returns `f"{self.name}: area={self.area():.2f}, perimeter={self.perimeter():.2f}"`.

## Module 2: circle (`shapes/circle.py`)

Depends on module 1.

- `shapes/circle.py`: class `Circle(Shape)`. `Circle(radius: float)`; `name` is `"circle"`; `area()` is `math.pi * radius ** 2` and `perimeter()` is `2 * math.pi * radius`. A radius of 0 or less raises `ValueError`.

## Module 3: rectangles (`shapes/rect.py`)

Depends on module 1. Modules 2 and 3 are independent: neither imports the other.

- `shapes/rect.py`: class `Rectangle(Shape)`. `Rectangle(width: float, height: float)`; `name` is `"rectangle"`; `area()` is `width * height` and `perimeter()` is `2 * (width + height)`. A width or height of 0 or less raises `ValueError`. Class `Square(Rectangle)`: `Square(side: float)` is a rectangle with equal sides, and its `name` is `"square"`.

## Module 4: command (`shapes_cli.py`)

Depends on modules 2 and 3.

- `shapes_cli.py`: `main(argv: list[str] | None = None) -> int`. The first argument picks the shape: `circle RADIUS`, `rect WIDTH HEIGHT`, or `square SIDE`. It builds the shape, prints `shape.describe()` to stdout, writes the same line to `shapes.txt` in the current folder, and returns 0. An unknown shape, a wrong number of arguments, a value that is not a number, or a value the shape rejects prints `error: <reason>` to stderr and returns 2. `python shapes_cli.py` runs `main()`.
