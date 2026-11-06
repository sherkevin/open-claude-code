"""Loopback Streamable HTTP MCP for the synthetic industrial fixture.

The service is read-only and backed only by ``fixtures/synthetic-industrial-v1``.
It is an acceptance fixture; it must never be pointed at customer or production
data.  The offline benchmark runner remains the request-free verification path.
"""
from __future__ import annotations

import argparse
import ipaddress
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("SYNTHETIC_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SYNTHETIC_MCP_PORT", "8777")))
    args = parser.parse_args()
    if not is_loopback(args.host):
        raise SystemExit("synthetic MCP must bind to loopback")
    if not 1 <= args.port <= 65535:
        raise SystemExit("--port must be between 1 and 65535")

    try:
        import uvicorn
        from mcp.server.fastmcp import FastMCP
        from mcp.server.transport_security import TransportSecuritySettings
        from mcp.types import ToolAnnotations
    except ImportError as exc:  # pragma: no cover - dependency is server-runtime owned
        raise SystemExit(f"install the server test environment before starting the fixture: {exc}") from exc

    from scripts.synthetic_benchmark import SyntheticIndustrialMCP, load_fixture

    manifest, _ = load_fixture()
    fixture = SyntheticIndustrialMCP(ROOT / "fixtures" / "synthetic-industrial-v1", manifest)
    allowed_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    server = FastMCP(
        "synthetic-industrial-v1",
        instructions="Read-only synthetic industrial fixture. Every result is synthetic and must not be treated as customer data.",
        host=args.host,
        port=args.port,
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        log_level="WARNING",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
            allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
        ),
    )

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False))
    def search_knowledge(query: str) -> dict:
        return fixture.search_knowledge(query)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False))
    def query_quality_metrics() -> dict:
        return fixture.query_quality_metrics()

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False))
    def inspect_image() -> dict:
        return fixture.inspect_image()

    print({"service": "synthetic-industrial-v1", "synthetic": True, "fixture_version": manifest["fixture_version"], "host": args.host, "port": args.port, "path": "/mcp"}, flush=True)
    uvicorn.run(server.streamable_http_app(), host=args.host, port=args.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
