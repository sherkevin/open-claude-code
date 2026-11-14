"""Real local MCP transport: representation and response-size boundaries."""
import asyncio
import json

import pytest

from test_harness_mcp_tools import completed, mcp_server, request, system  # noqa: F401


@pytest.mark.parametrize("mode", ["both", "only", "empty"])
def test_structured_payload_reaches_model_once_without_losing_sources(system, mode):
    system.server.structured_mode = mode
    result = asyncio.run(system.runtime.bridge.invoke(request(system)))
    assert not result.get("is_error"), result
    payload_blocks = [block for block in result["content"]
                      if json.loads(block["text"]) == result["structuredContent"]]
    assert len(payload_blocks) == 1
    assert completed(system)["success"] is True
    if mode == "empty":
        assert result["structuredContent"] == {} and completed(system)["source_ids"] == []
    else:
        sources = system.service.evidence.list(system.actor.current_run_id, system.owner)
        assert len(sources) == 1 and completed(system)["source_ids"] == [sources[0]["id"]]
        assert completed(system)["result_facts"]["business_domain"] == "制造运营"


def test_valid_transport_result_is_not_rejected_by_host_added_duplicate(system):
    # Wire result contains both representations (<1MB). The old normalization
    # appended a third copy and incorrectly pushed it over the same limit.
    system.server.payload_padding = 400_000
    result = asyncio.run(system.runtime.bridge.invoke(request(system)))
    assert not result.get("is_error"), result
    text = "\n".join(block["text"] for block in result["content"])
    assert text.count('"padding"') == 1
    assert len(result["structuredContent"]["padding"]) == 400_000
    assert completed(system)["success"] is True


def test_wire_size_limit_remains_enforced_without_truncation_or_sources(system):
    system.server.payload_padding = 600_000
    result = asyncio.run(system.runtime.bridge.invoke(request(system)))
    assert result.get("is_error") is True
    assert completed(system)["success"] is False
    assert completed(system)["source_ids"] == []
    assert not system.service.evidence.list(system.actor.current_run_id, system.owner)
