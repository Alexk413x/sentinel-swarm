import os
import sys

__version__ = "0.0.1"

# uv has already used this venv; a child uv run, such as codebase-kg's or the host's test
# command, would otherwise sync its own project into the ledger's venv.
_venv = os.environ.get("UV_PROJECT_ENVIRONMENT")
if _venv and os.path.normcase(os.path.abspath(_venv)) == os.path.normcase(sys.prefix):
    del os.environ["UV_PROJECT_ENVIRONMENT"]
