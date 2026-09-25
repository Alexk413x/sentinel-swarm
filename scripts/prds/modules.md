# PRD: hello and world

Python 3.10+, standard library only. Every file with code has pytest tests under `tests/`.

## Module 1: hello (`hello/`)

- `hello/__init__.py`: package marker.
- `hello/hello.py`: `hello() -> str` returns `"Hello"`.

## Module 2: world (`world/`)

- `world/__init__.py`: package marker.
- `world/world.py`: `world() -> str` returns `"World"`.

Modules 1 and 2 are independent: neither imports the other.

## Module 3: command (`hello_world.py`)

Depends on modules 1 and 2.

- `hello_world.py`: `main() -> int` builds the line `f"{hello()}, {world()}!"`, writes it to `hello_world.txt` in the current folder, prints it to stdout, and returns 0. `python hello_world.py` runs `main()`.
