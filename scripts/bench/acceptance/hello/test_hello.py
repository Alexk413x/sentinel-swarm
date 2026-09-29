from __future__ import annotations

from pathlib import Path


def test_running_the_script_writes_hello_world(host: Path, run_python, tmp_path: Path) -> None:
    scripts = [name for name in ("hello.py", "hello_world.py") if (host / name).is_file()]
    assert scripts, "neither hello.py nor hello_world.py exists"
    done = run_python(str(host / scripts[0]))
    assert done.returncode == 0, done.stderr
    output = tmp_path / "hello_world.txt"
    assert output.is_file(), "the script wrote no hello_world.txt in the current folder"
    assert "Hello, world!" in output.read_text(encoding="utf-8")
