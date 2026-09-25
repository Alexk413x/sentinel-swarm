# PRD: hello, world, and name

Python 3.10+, standard library only. Every file with code has pytest tests under `tests/`.

## Module 1: hello (`hello/`)

- `hello/__init__.py`: package marker.
- `hello/hello.py`: `hello() -> str` returns `"Hello"`.

## Module 2: world (`world/`)

- `world/__init__.py`: package marker.
- `world/world.py`: class `World` with a method `text(self) -> str` that returns `"World"`.

Modules 1 and 2 are independent: neither imports the other.

## Module 3: name (`name/`)

Depends on module 2.

- `name/__init__.py`: package marker.
- `name/name.py`: class `Name(World)`. `Name(name: str)` stores the name, and `text()` returns it in place of `"World"`. An empty or blank name raises `ValueError`.

## Module 4: command (`hello_world.py`)

Depends on modules 1, 2, and 3.

- `hello_world.py`: `main(argv: list[str] | None = None) -> int`. An optional `--name NAME` argument picks `Name(NAME)`; without it the command uses `World()`. It builds the line `f"{hello()}, {subject.text()}!"`, writes it to `hello_world.txt` in the current folder, prints it to stdout, and returns 0. An invalid name prints `error: <reason>` to stderr and returns 2. `python hello_world.py` runs `main()`.
