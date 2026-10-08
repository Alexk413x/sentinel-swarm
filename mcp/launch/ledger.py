# Standard library only: it runs on the base interpreter with -I -S and puts the ledger's
# src folder on sys.path, so the plugin needs no venv.
import re
import runpy
import sys
from pathlib import Path

if sys.version_info < (3, 9):  # noqa: UP036
    sys.exit("sentinel-swarm needs Python 3.9 or newer")
if len(sys.argv) < 2 or not re.fullmatch(r"[a-z_]+", sys.argv[1]):
    sys.exit("usage: ledger.py <module> [args], such as ledger.py setup")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
runpy.run_module(f"swarm_ledger.{sys.argv.pop(1)}", run_name="__main__", alter_sys=True)
