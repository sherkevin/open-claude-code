"""Host nonce bridge and approval receipt tests (no SDK/client required)."""
import asyncio
import sys
import time
import types
from types import SimpleNamespace

import pytest

from app.capabilities import (
    CAPABILITY_TICKET_FIELD,
    _capability_handler,
)
from app.external_capabilities import record_approval_receipt
from app.runtime import ApprovalReceipt, SDKRuntime
from app.security import AppError
from app.workspace_tools import APPROVAL_ID, TOOL_CALL_ID, WorkspaceTool


def bare_runtime():
    runtime = SDKRuntime.__new__(SDKRuntime)
    runtime.current_run = {"id": "run-1"}
    runtime._capability_tickets = {}
    runtime._capability_ticket_cap = 128
    return runtime


def definition(handler=None, name="ocr"):
    async def default(_arguments):
        return {"content": [{"type": "text", "text": "ok"}]}
    return WorkspaceTool(
        name=name, description="test", input_schema={"file_id": str, "purpose": str},
        read_only=True, handler=handler or default,
    )


def test_approval_receipt_keeps_boolean_callers_working():
    assert bool(ApprovalReceipt(True)) is True
    assert bool(ApprovalReceipt(True, "approval-1", "user")) is True
    assert bool(ApprovalReceipt(False)) is False
    assert ApprovalReceipt(True, "approval-1", "user").approval_id == "approval-1"
    assert ApprovalReceipt(True, "approval-1", "user").source == "user"


def test_exact_approval_decision_becomes_a_one_time_receipt(monkeypatch):
    module = types.ModuleType("app.capability_receipts")
    calls = []

    def issue_approval_receipt(service, **kwargs):
        calls.append((service, kwargs))
        return "receipt-1"

    module.issue_approval_receipt = issue_approval_receipt
    monkeypatch.setitem(sys.modules, "app.capability_receipts", module)
    service = object()
    receipt = ApprovalReceipt(True, "approval-row-1", "user")
    value = record_approval_receipt(
        service, run_id="run-1", tool_call_id="toolu-1",
        tool_name="mcp__workspace__ocr", arguments={"file_id": "f1"}, receipt=receipt)
    assert value == "receipt-1"
    called_service, kwargs = calls[0]
    assert called_service is service
    assert kwargs == {
        "run_id": "run-1", "tool_call_id": "toolu-1", "tool_name": "mcp__workspace__ocr",
        "arguments": {"file_id": "f1"}, "decision": "allowed", "source": "user",
        "approval_id": "approval-row-1",
    }


@pytest.mark.parametrize("receipt", [True, ApprovalReceipt(True), ApprovalReceipt(True, "a", "host")])
def test_receipt_issuance_rejects_bare_boolean_or_unknown_source(receipt):
    with pytest.raises(AppError) as error:
        record_approval_receipt(object(), run_id="r", tool_call_id="t",
                                tool_name="mcp__workspace__ocr", arguments={}, receipt=receipt)
    assert error.value.code == "APPROVAL_RECEIPT_REQUIRED"


def test_receipt_issuance_fails_closed_without_executor_contract(monkeypatch):
    # Setting the module to None makes the deferred import raise ImportError
    # even when the real capability_receipts module exists (integration tree).
    monkeypatch.setitem(sys.modules, "app.capability_receipts", None)
    with pytest.raises(AppError) as error:
        record_approval_receipt(object(), run_id="r", tool_call_id="t",
                                tool_name="mcp__workspace__ocr", arguments={},
                                receipt=ApprovalReceipt(True, "a", "dual"))
    assert error.value.code == "CAPABILITY_EXECUTOR_UNAVAILABLE"


def test_nonce_is_one_time_and_bound_to_tool_arguments_and_run():
    runtime = bare_runtime()
    arguments = {"file_id": "f1"}
    ticket = runtime.issue_capability_ticket(
        "mcp__workspace__ocr", arguments, SimpleNamespace(tool_use_id="toolu-1"), "approval-1")
    assert ticket and len(ticket) >= 32

    assert runtime.consume_capability_ticket(
        ticket, "mcp__workspace__image_search", arguments) is None  # wrong tool
    # A wrong-tool attempt consumed the ticket: the token is single-use even
    # when the binding does not match.
    assert runtime.consume_capability_ticket(ticket, "mcp__workspace__ocr", arguments) is None

    ticket = runtime.issue_capability_ticket(
        "mcp__workspace__ocr", arguments, SimpleNamespace(tool_use_id="toolu-2"), None)
    assert runtime.consume_capability_ticket(
        ticket, "mcp__workspace__ocr", {"file_id": "f2"}) is None  # wrong args
    assert runtime.consume_capability_ticket(ticket, "mcp__workspace__ocr", arguments) is None

    ticket = runtime.issue_capability_ticket(
        "mcp__workspace__ocr", arguments, SimpleNamespace(tool_use_id="toolu-3"), None)
    binding = runtime.consume_capability_ticket(ticket, "mcp__workspace__ocr", arguments)
    assert binding["tool_use_id"] == "toolu-3" and binding["approval_id"] is None
    assert runtime.consume_capability_ticket(ticket, "mcp__workspace__ocr", arguments) is None


def test_nonce_requires_real_tool_call_id_and_expires():
    runtime = bare_runtime()
    arguments = {"file_id": "f1"}
    assert runtime.issue_capability_ticket(
        "mcp__workspace__ocr", arguments, SimpleNamespace(tool_use_id=None)) is None
    assert runtime.issue_capability_ticket(
        "mcp__workspace__ocr", arguments, SimpleNamespace(tool_use_id="")) is None
    runtime.current_run = None
    assert runtime.issue_capability_ticket(
        "mcp__workspace__ocr", arguments, SimpleNamespace(tool_use_id="toolu-4")) is None
    runtime.current_run = {"id": "run-1"}
    ticket = runtime.issue_capability_ticket(
        "mcp__workspace__ocr", arguments, SimpleNamespace(tool_use_id="toolu-5"))
    runtime._capability_tickets[ticket]["expires_at"] = time.monotonic() - 1
    assert runtime.consume_capability_ticket(ticket, "mcp__workspace__ocr", arguments) is None


def test_dispatch_strips_ticket_and_sets_host_context():
    seen = {}

    async def handler(arguments):
        seen["arguments"] = arguments
        seen["call_id"] = TOOL_CALL_ID.get()
        seen["approval_id"] = APPROVAL_ID.get()
        return {"content": [{"type": "text", "text": "ok"}]}

    runtime = bare_runtime()
    public = {"file_id": "f1"}
    ticket = runtime.issue_capability_ticket(
        "mcp__workspace__ocr", public, SimpleNamespace(tool_use_id="toolu-real"), "approval-9")
    dispatch = _capability_handler(definition(handler), runtime)
    result = asyncio.run(dispatch({**public, CAPABILITY_TICKET_FIELD: ticket}))
    assert not result.get("is_error")
    assert seen == {"arguments": public, "call_id": "toolu-real", "approval_id": "approval-9"}
    # Context variables do not leak into the caller.
    assert TOOL_CALL_ID.get() is None and APPROVAL_ID.get() is None
    # Replay performs zero handler work.
    replay = asyncio.run(dispatch({**public, CAPABILITY_TICKET_FIELD: ticket}))
    assert replay["is_error"] is True and seen["arguments"] == public


def test_dispatch_requires_ticket_and_rejects_unknown_public_parameters():
    calls = []

    async def handler(arguments):
        calls.append(arguments)
        return {"content": []}

    runtime = bare_runtime()
    dispatch = _capability_handler(definition(handler), runtime)
    missing = asyncio.run(dispatch({"file_id": "f1"}))
    assert missing["is_error"] is True and calls == []
    ticket = runtime.issue_capability_ticket(
        "mcp__workspace__ocr", {"file_id": "f1", "extra": "x"},
        SimpleNamespace(tool_use_id="toolu-6"))
    assert ticket
    # Unknown public key is rejected before the nonce is consumed.
    rejected = asyncio.run(dispatch({"file_id": "f1", "extra": "x", CAPABILITY_TICKET_FIELD: ticket}))
    assert rejected["is_error"] is True and calls == []
    assert runtime._capability_tickets  # ticket not burned by an invalid request
    wrong_type = asyncio.run(dispatch({"file_id": 1, CAPABILITY_TICKET_FIELD: ticket}))
    assert wrong_type["is_error"] is True and calls == []


def test_dispatch_without_consume_support_fails_closed():
    async def handler(arguments):
        raise AssertionError("handler must not run")

    dispatch = _capability_handler(definition(handler), SimpleNamespace())
    result = asyncio.run(dispatch({CAPABILITY_TICKET_FIELD: "ticket"}))
    assert result["is_error"] is True
