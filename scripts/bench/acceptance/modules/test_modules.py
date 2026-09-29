from __future__ import annotations

from pathlib import Path

import pytest


def test_hello_returns_hello() -> None:
    from hello.hello import hello

    assert hello() == "Hello"


def test_world_text() -> None:
    from world.world import World

    assert World().text() == "World"


def test_name_subclasses_world() -> None:
    from name.name import Name
    from world.world import World

    assert issubclass(Name, World)
    assert Name("Ada").text() == "Ada"


@pytest.mark.parametrize("bad", ["", "   "])
def test_blank_name_raises(bad: str) -> None:
    from name.name import Name

    with pytest.raises(ValueError):
        Name(bad)


def test_main_without_a_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import hello_world

    monkeypatch.chdir(tmp_path)
    assert hello_world.main([]) == 0
    assert "Hello, World!" in (tmp_path / "hello_world.txt").read_text(encoding="utf-8")
    assert "Hello, World!" in capsys.readouterr().out


def test_main_with_a_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import hello_world

    monkeypatch.chdir(tmp_path)
    assert hello_world.main(["--name", "Ada"]) == 0
    assert "Hello, Ada!" in (tmp_path / "hello_world.txt").read_text(encoding="utf-8")
    assert "Hello, Ada!" in capsys.readouterr().out


def test_main_with_a_blank_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import hello_world

    monkeypatch.chdir(tmp_path)
    assert hello_world.main(["--name", " "]) == 2
    assert capsys.readouterr().err.startswith("error: ")


def test_the_command_runs_as_a_script(host: Path, run_python, tmp_path: Path) -> None:
    done = run_python(str(host / "hello_world.py"), "--name", "Grace")
    assert done.returncode == 0, done.stderr
    assert "Hello, Grace!" in done.stdout
    assert "Hello, Grace!" in (tmp_path / "hello_world.txt").read_text(encoding="utf-8")
