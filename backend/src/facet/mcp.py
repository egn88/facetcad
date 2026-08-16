"""Composition root for the MCP server, so it runs as ``python -m facet.mcp``.

The counterpart to :mod:`facet.main`, and just as small. This one wires up a
URL rather than a kernel and a repository, because the MCP adapter reaches the
application layer over HTTP — :mod:`facet.adapters.mcp.server` says why.
"""

from __future__ import annotations

from facet.adapters.mcp.server import build_server


def main() -> None:
    build_server().run("stdio")


if __name__ == "__main__":
    main()
