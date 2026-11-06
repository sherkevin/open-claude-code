"""A real MCP server used only by the local transport integration test."""
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("transport-test", host="127.0.0.1", port=int(sys.argv[1]), stateless_http=True)


@mcp.tool()
def lookup(query: str) -> str:
    """Look up test data."""
    Path(sys.argv[2]).write_text("called", encoding="utf-8")
    return query


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
