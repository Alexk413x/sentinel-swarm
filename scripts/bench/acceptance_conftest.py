from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

HOST = Path(os.environ["BENCH_HOST"]).resolve()
sys.path.insert(0, str(HOST))

Runner = Callable[..., subprocess.CompletedProcess[str]]


@pytest.fixture
def host() -> Path:
    return HOST


@pytest.fixture
def run_python(tmp_path: Path) -> Runner:
    def run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ, PYTHONPATH=str(HOST), PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run(
            [sys.executable, *args],
            cwd=cwd or tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    return run
