"""Offline regressions for MCP metadata and serialized credential boundaries."""

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest
from jsonschema.exceptions import ValidationError

from app.mcp_tools import MCPBusinessTool, schema_validator, transport_logs
from app.security import AppError


def business_tool(credential, schema, *, connection_name="业务连接"):
    runtime = SimpleNamespace(
        config={"mcp_servers": {"c1": {"url": "https://example.invalid/mcp"}}},
        service=SimpleNamespace(secrets=SimpleNamespace(decrypt=lambda _: credential)),
    )
    return MCPBusinessTool(runtime, {"id": "1", "name": connection_name, "secret": "encrypted-fixture"}, {
        "name": "read_record", "description": "Synthetic business tool", "inputSchema": schema,
    })


@pytest.mark.parametrize("credential", ['synthetic"quoted-secret', "synthetic\\slash-secret"])
@pytest.mark.parametrize("location", ["default", "enum", "property_name"])
def test_schema_cannot_serialize_escaped_transport_credential(credential, location):
    property_schema = {"type": "string"}
    schema = {"type": "object", "properties": {"record": property_schema}}
    if location == "property_name":
        schema["properties"][credential] = {"type": "string"}
    elif location == "enum":
        property_schema[location] = [credential]
    else:
        property_schema[location] = credential
    with pytest.raises(AppError) as failure:
        business_tool(credential, schema)
    assert failure.value.code == "HARNESS_MCP_SCHEMA_UNSUPPORTED"


@pytest.mark.parametrize("layers", [1, 2, 3])
def test_schema_cannot_hide_credential_inside_encoded_metadata(layers):
    credential = 'synthetic"encoded-schema-secret'
    value = credential
    for _ in range(layers):
        value = json.dumps(value)
    with pytest.raises(AppError) as failure:
        business_tool(credential, {"type": "object", "properties": {
            "record": {"type": "string", "default": value},
        }})
    assert failure.value.code == "HARNESS_MCP_SCHEMA_UNSUPPORTED"


def test_schema_business_field_names_and_constants_remain_unchanged():
    schema = {"type": "object", "properties": {
        "token": {"type": "string", "enum": ["public-record-identifier"]},
        "kind": {"const": "production-record"},
    }, "required": ["token", "kind"], "additionalProperties": False}
    expected = json.loads(json.dumps(schema))
    tool = business_tool("synthetic-unrelated-transport-secret", schema)
    assert tool.definition()["input_schema"] == expected
    arguments = {"token": "public-record-identifier", "kind": "production-record"}
    assert tool.arguments(arguments) == arguments


def test_model_tool_description_preserves_safe_source_label_and_raw_name():
    credential = "synthetic-connection-secret"
    url = "https://example.invalid/mcp"
    tool = business_tool(credential, {"type": "object"}, connection_name=f"MCP 3000\n{url} {credential}")
    alias = business_tool(credential, {"type": "object"}, connection_name="Renamed MCP")
    description = tool.definition()["description"]
    assert '"MCP 3000 [redacted] [redacted]"' in description
    assert '"read_record"' in description and "Synthetic business tool" in description
    assert credential not in description and url not in description
    assert description.count("\n") == 1
    assert tool.definition()["name"] == alias.definition()["name"]


@pytest.mark.parametrize("credential", ['synthetic"quoted-secret', "synthetic\\slash-secret"])
@pytest.mark.parametrize("layers", [1, 2, 3])
def test_json_scalar_and_nested_strings_cannot_echo_transport_credential(credential, layers):
    tool = business_tool(credential, {"type": "object"})
    value = credential
    for _ in range(layers):
        value = json.dumps(value)
    result = tool.clean({"content": [{"type": "text", "text": value}],
                         "structuredContent": {"description": value}})
    for text in [result["content"][0]["text"], result["structuredContent"]["description"]]:
        for _ in range(layers + 1):
            assert credential not in text
            try:
                text = json.loads(text)
            except ValueError:
                break
            assert isinstance(text, str)
        assert "redacted" in text


@pytest.mark.parametrize("dialect", [
    "http://json-schema.org/draft-07/schema#",
    "https://json-schema.org/draft/2019-09/schema",
    "https://json-schema.org/draft/2020-12/schema",
])
def test_supported_dialects_preserve_local_reference_constraints(dialect):
    validator = schema_validator({
        "$schema": dialect, "type": "object",
        "definitions": {"record": {"type": "integer", "minimum": 1}},
        "properties": {"record": {"$ref": "#/definitions/record"}},
        "required": ["record"], "additionalProperties": False,
    })
    validator.validate({"record": 1})
    for invalid in ({"record": 0}, {"record": "1"}, {}, {"record": 1, "extra": True}):
        with pytest.raises(ValidationError):
            validator.validate(invalid)


@pytest.mark.parametrize("dialect", ["https://example.invalid/schema", 1, None])
def test_unknown_dialects_fail_before_any_operation(dialect):
    with pytest.raises(AppError) as failure:
        schema_validator({"$schema": dialect, "type": "object"})
    assert failure.value.code == "HARNESS_MCP_SCHEMA_UNSUPPORTED"


@pytest.mark.parametrize("reference", ["https://example.invalid/schema", "file:///tmp/schema", "relative.json"])
def test_schema_external_references_fail_before_any_operation(reference):
    with pytest.raises(AppError) as failure:
        schema_validator({"type": "object", "properties": {"record": {"$ref": reference}}})
    assert failure.value.code == "HARNESS_MCP_SCHEMA_UNSUPPORTED"


def test_transport_logging_hides_actual_upstream_parse_errors_then_restores_scope(caplog):
    import httpx
    from mcp.client.streamable_http import StreamableHTTPTransport

    marker = "synthetic-protocol-credential-marker"
    failures = []

    class Sink:
        async def send(self, value):
            failures.append(value)

    async def scenario():
        transport = StreamableHTTPTransport("http://127.0.0.1:9/mcp")
        # The parser is official SDK code; this test never opens a socket.
        response = httpx.Response(200, json={"jsonrpc": marker, "id": 1, "result": {"text": marker}})
        with transport_logs():
            await transport._handle_json_response(response, Sink(), False)

    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())
        records = list(caplog.records)
        assert failures and records
        assert all(marker not in record.getMessage() for record in records)
        assert all(record.exc_info is None and record.exc_text is None and record.stack_info is None for record in records)
        assert any(record.name == "mcp.client.streamable_http" and record.levelno == logging.ERROR for record in records)
        logger = logging.getLogger("mcp.client.streamable_http")
        logger.warning("outside-log-remains-visible")
        assert caplog.records[-1].getMessage() == "outside-log-remains-visible"
