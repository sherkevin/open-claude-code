import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from app.model_followups import _context, _questions, conversation_followups, provider_followups
from test_api import PASSWORD, client, configured, conversation, login, poll, send


def test_followups_authorize_cache_and_ignore_old_turns(client, monkeypatch):
    calls = []

    async def provider(service, context):
        calls.append(context)
        return {"suggestions": ["按客户比较订单金额？"], "router": {"mode": "authorized"}}

    monkeypatch.setattr("app.model_followups.provider_followups", provider)
    cid = conversation(client)["id"]
    rid = send(client, cid)["run_id"]
    assert poll(client, rid)["status"] == "succeeded"
    payload = {"conversation_id": cid, "run_id": rid}
    first = client.post("/api/v1/models/followups", json=payload)
    assert first.status_code == 200
    assert first.json()["suggestions"] == ["按客户比较订单金额？"]
    cached = client.post("/api/v1/models/followups", json=payload).json()["router"]
    assert cached["cached"] is True and cached["provider_attempted"] is False
    assert cached["usage"] == {"input_tokens": 0, "output_tokens": 0}
    assert len(calls) == 1
    client.app.state.service.create_user({"username": "other", "display_name": "Other", "password": PASSWORD, "role": "member"})
    login(client, "other")
    assert client.post("/api/v1/models/followups", json=payload).status_code == 404
    login(client)
    other_cid = conversation(client)["id"]
    assert client.post("/api/v1/models/followups", json={**payload, "conversation_id": other_cid}).status_code == 404
    send(client, cid, request_id="second-request")
    old = client.post("/api/v1/models/followups", json=payload).json()
    assert old["suggestions"] == [] and old["router"]["mode"] == "not_current"
    assert len(calls) == 1


def test_followups_disabled_route_has_no_invented_question(client):
    cid = conversation(client)["id"]
    rid = send(client, cid)["run_id"]
    poll(client, rid)
    result = client.post("/api/v1/models/followups", json={"conversation_id": cid, "run_id": rid})
    assert result.status_code == 200
    assert result.json()["suggestions"] == []
    assert result.json()["router"]["provider_attempted"] is False


def test_context_is_bounded_and_question_output_is_plain_text():
    service = SimpleNamespace(db=SimpleNamespace(one=lambda *_: {"text": "token=credential123 " + "业务数据" * 2000}))
    context = _context(service, {"message_id": "q", "assistant_message_id": "a", "conversation_id": "c"})
    assert sum(map(len, context.values())) <= 1400
    assert "credential123" not in json.dumps(context)
    assert _questions({"suggestions": ["查看客户订单？", "查看客户订单？", "<script>test</script>"]}) == ["查看客户订单？"]
    assert _questions({"suggestions": ["token=secret123", "https://example.com", 3]}) == []
    assert _questions([]) == []


def test_parallel_followups_share_one_provider_request(client, monkeypatch):
    cid = conversation(client)["id"]
    rid = send(client, cid)["run_id"]
    poll(client, rid)
    service = client.app.state.service
    user = service.db.one("SELECT * FROM users WHERE username='admin'")
    calls = []

    async def provider(*_):
        calls.append(True)
        await asyncio.sleep(.02)
        return {"suggestions": ["还有哪些异常订单？"], "router": {"mode": "authorized"}}

    monkeypatch.setattr("app.model_followups.provider_followups", provider)

    async def run():
        return await asyncio.gather(*(conversation_followups(service, cid, rid, user) for _ in range(3)))

    values = asyncio.run(run())
    assert len(calls) == 1 and all(value["suggestions"] for value in values)
    assert sum(bool(value["router"].get("shared")) for value in values) == 2


def test_stale_response_is_discarded(client, monkeypatch):
    cid = conversation(client)["id"]
    rid = send(client, cid)["run_id"]
    poll(client, rid)

    async def provider(service, context):
        service.db.execute("UPDATE conversations SET archived=1 WHERE id=?", (cid,))
        return {"suggestions": ["按客户比较订单金额？"], "router": {"mode": "authorized"}}

    monkeypatch.setattr("app.model_followups.provider_followups", provider)
    result = client.post("/api/v1/models/followups", json={"conversation_id": cid, "run_id": rid}).json()
    assert result["suggestions"] == [] and result["router"]["mode"] == "not_current"


@pytest.mark.parametrize("body,expected", [
    ({"content": [{"text": '{"suggestions":["哪些订单需要优先关注？"]}'}], "usage": {"input_tokens": 111, "output_tokens": 22}}, ["哪些订单需要优先关注？"]),
    ([], []),
    ({"content": [{"text": 'not JSON'}]}, []),
])
def test_provider_contract_is_bounded_and_fail_closed(monkeypatch, body, expected):
    import app.model_followups as module
    service = SimpleNamespace(settings=SimpleNamespace(tab_completion_enabled=True, model_router_budget_usd=1, model_router_max_output_tokens=999, model_router_timeout_seconds=999))
    service.effective_provider = lambda _: {"ANTHROPIC_API_KEY": "test-only", "ANTHROPIC_BASE_URL": "https://provider.test/v1"}
    monkeypatch.setattr(module, "_tab_target", lambda _: ("public", {"upstream_model": "authorized"}, None))
    monkeypatch.setattr(module, "provider_route", lambda _: SimpleNamespace(production_ready=True))
    original_client = httpx.AsyncClient
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=body)

    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kwargs: original_client(**kwargs, transport=httpx.MockTransport(respond)))
    result = asyncio.run(provider_followups(service, {"question": "查询订单", "answer": "已汇总"}))
    assert result["suggestions"] == expected
    assert len(requests) == 1 and requests[0]["max_tokens"] == 256
    assert result["router"]["provider_attempted"] is True
