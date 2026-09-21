from __future__ import annotations

from fastmcp import FastMCP

from . import __version__

mcp = FastMCP("swarm-ledger")


@mcp.tool
def ledger_info() -> dict[str, str]:
    return {"name": "swarm-ledger", "version": __version__, "status": "skeleton"}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
