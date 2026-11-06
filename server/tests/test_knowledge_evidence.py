import asyncio
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import dump, now, uid
from app.main import create_app
from app.security import AppError


@pytest.fixture
def system(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_KNOWLEDGE_EMBEDDING_MODEL", "local-lexical-v1")
    app = create_app(Settings(data_dir=tmp_path, provider_settings="", model="test"))
    with TestClient(app) as client:
        service = app.state.service
        users = {}
        for name, role in (("owner", "member"), ("peer", "member"), ("admin", "admin")):
            users[name] = service.create_user({"username": name, "display_name": name, "password": "test-password-123", "role": role})
        yield client, service, users


def login(client, name):
    assert client.post("/api/v1/auth/login", json={"username": name, "password": "test-password-123"}).status_code == 200


def setup_run(service, user, kb_ids=None, connections=None, web=False, conversation=None):
    assistant = service.save_assistant({"name": "Knowledge assistant", "knowledge_base_ids": kb_ids or [], "connections": connections or [], "web_search_enabled": web}, user=user)
    conversation = conversation or service.create_conversation({"assistant_id": assistant["id"], "title": "test"}, user)
    run_id = uid()
    service.db.execute("INSERT INTO runs(id,conversation_id,user_id,client_request_id,message_id,status,created_at) VALUES(?,?,?,?,?,'running',?)", (run_id, conversation["id"], user["id"], uid(), uid(), now()))
    return assistant, conversation, run_id


def test_personal_and_team_knowledge_api_authorization(system):
    client, service, users = system
    login(client, "owner")
    assert client.post("/api/v1/knowledge-bases", json={"name": "Cannot share", "scope": "team"}).status_code == 403
    result = client.post("/api/v1/knowledge-bases", json={"name": "Private manual"})
    assert result.status_code == 201
    kb = result.json()
    assert kb["owner_id"] == users["owner"]["id"] and kb["can_edit"]
    file = client.post(f"/api/v1/knowledge-bases/{kb['id']}/documents", files={"file": ("spec.md", "# Manufacturing\n\nPrivate tolerance 0.07 mm.".encode(), "text/markdown")}).json()
    assert file["status"] == "ready"
    for name in ("peer", "admin"):
        login(client, name)
        assert client.get("/api/v1/knowledge-bases").json() == []
        assert client.get(file["content_url"]).status_code == 404
        assert client.get(f"/api/v1/knowledge-documents/{file['id']}/chunks").status_code == 404
        assert client.post(f"/api/v1/knowledge-bases/{kb['id']}/search", json={"query": "tolerance"}).status_code == 404
    shared = service.knowledge.save({"name": "Team", "scope": "team", "default_for_new_assistants": True}, users["admin"])
    login(client, "peer")
    assert [item["id"] for item in client.get("/api/v1/knowledge-bases").json()] == [shared["id"]]
    assert client.patch(f"/api/v1/knowledge-bases/{shared['id']}", json={"name": "bad"}).status_code == 404
    assistant = service.save_assistant({"name": "Default knowledge"}, user=users["peer"])
    assert assistant["knowledge_base_ids"] == [shared["id"]]


def test_original_versions_retrieval_and_historical_citation(system):
    client, service, users = system
    user = users["owner"]
    kb = service.knowledge.save({"name": "工艺手册"}, user)
    original = "# 设备校验\n\n螺栓扭矩设定为 37 Nm，检查间隔为每周一次。".encode()
    first = service.knowledge.upload(kb["id"], user, "工艺.md", original)
    assistant, conversation, run_id = setup_run(service, user, [kb["id"]])
    found = asyncio.run(service.knowledge.search_for_run(user, assistant, run_id, "螺栓扭矩"))
    assert found["results"] and "37 Nm" in found["results"][0]["text"]
    item = found["results"][0]
    final = service.evidence.finalize(run_id, "设定为 37 Nm。" + item["citation"])
    assert len(final["citations"]) == 1 and service.evidence.get(final["citations"][0]["id"], user)["locator"]["paragraph"] == 2
    assert service.evidence.get(final["citations"][0]["id"], user)["locator"]["page"] is None
    assert 'excerpt' not in final['citations'][0] and 'locator' not in final['citations'][0]
    assert final["citations"][0]["document_version"] == 1
    second = service.knowledge.upload(kb["id"], user, "工艺.md", "# 设备校验\n\n螺栓扭矩改为 45 Nm。".encode(), first["id"])
    assert second["version"] == 2
    latest = service.knowledge.search([kb["id"]], user, "螺栓扭矩")
    assert latest["results"][0]["version"] == 2 and "45 Nm" in latest["results"][0]["text"]
    login(client, "owner")
    assert client.get(first["content_url"]).content == original
    source = client.get(final["citations"][0]["url"]).json()
    assert source["locator"]["sha256"] == first["sha256"]
    assert "37 Nm" in source["excerpt"]
    assert len(client.get(f"/api/v1/knowledge-documents/{first['id']}/versions").json()) == 2
    assert all(item["version"] == 1 for item in client.get(f"/api/v1/knowledge-documents/{first['id']}/chunks?version=1").json())


def test_forged_cross_conversation_and_revoked_sources(system):
    _, service, users = system
    user = users["owner"]
    kb = service.knowledge.save({"name": "Limits"}, user)
    doc = service.knowledge.upload(kb["id"], user, "limits.txt", b"Maximum pressure is 7 bar.")
    assistant, conversation, run_id = setup_run(service, user, [kb["id"]])
    result = asyncio.run(service.knowledge.read_for_run(user, assistant, run_id, doc["id"], 1))
    source_id = result["results"][0]["source_id"]
    _, _, foreign_run = setup_run(service, user, [kb["id"]])
    assert service.evidence.finalize(foreign_run, f"Unsafe[[source:{source_id}]]")['citations'] == []
    assert service.evidence.finalize(run_id, "Unsafe[[source:invented]] [9](/api/v1/sources/invented)")['citations'] == []
    # Historical continuation within the same owned conversation is explicitly copied.
    second_run = uid()
    service.db.execute("INSERT INTO runs(id,conversation_id,user_id,client_request_id,message_id,status,created_at) VALUES(?,?,?,?,?,'running',?)", (second_run, conversation["id"], user["id"], uid(), uid(), now()))
    carried = service.evidence.finalize(second_run, f"Still 7 bar[[source:{source_id}]]")
    assert len(carried['citations']) == 1 and carried['citations'][0]['id'] != source_id
    with pytest.raises(AppError):
        service.evidence.get(source_id, users['peer'])
    service.save_assistant({"knowledge_base_ids": []}, assistant['id'], user=user)
    with pytest.raises(AppError):
        service.evidence.get(source_id, user)
    assert service.evidence.finalize(second_run, f"Revoked[[source:{source_id}]]")['citations'] == []


def test_bad_documents_are_not_empty_ready_indices(system, monkeypatch):
    _, service, users = system
    user = users['owner']
    kb = service.knowledge.save({"name": "Broken"}, user)
    bad = service.knowledge.upload(kb['id'], user, 'bad.pdf', b'%PDF not a document')
    assert bad['status'] == 'failed' and bad['chunk_count'] == 0
    blank = service.knowledge.upload(kb['id'], user, 'blank.txt', b'\n  \n')
    assert blank['status'] == 'failed'
    def missing_ocr(*args):
        raise AppError(422, 'OCR_REQUIRED', 'OCR missing')
    monkeypatch.setattr(service.knowledge.parser, 'parse', missing_ocr)
    scanned = service.knowledge.upload(kb['id'], user, 'scan.pdf', b'fake-scan')
    assert scanned['status'] == 'needs_ocr'
    assert service.knowledge.search([kb['id']], user, 'document')['results'] == []


def test_docx_paragraph_extraction_and_xml_entity_rejection(system):
    _, service, users = system
    user = users['owner']
    kb = service.knowledge.save({'name': 'DOCX'}, user)
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as archive:
        archive.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Valve P1 tolerance 0.02 mm.</w:t></w:r></w:p></w:body></w:document>')
    document = service.knowledge.upload(kb['id'], user, 'manual.docx', data.getvalue())
    assert document['status'] == 'ready' and document['extraction'] == ['docx-paragraphs']
    assert 'Valve P1' in service.knowledge.chunks(document['id'], user)[0]['text']
    malicious = io.BytesIO()
    with zipfile.ZipFile(malicious, 'w') as archive:
        archive.writestr('word/document.xml', '<!DOCTYPE test [<!ENTITY file SYSTEM "file:///etc/passwd">]><test>&file;</test>')
    assert service.knowledge.upload(kb['id'], user, 'entity.docx', malicious.getvalue())['status'] == 'failed'


def test_data_provenance_preserves_actual_result_and_missing_fields(system):
    _, service, users = system
    user = users['owner']
    connection = service.save_connection({'name': 'Quality', 'url': 'http://127.0.0.1:8767/mcp', 'auth_type': 'none'}, user=user)
    service.db.execute('UPDATE connections SET tools=? WHERE id=?', (dump([{'name': 'quality_read'}]), connection['id']))
    assistant, _, run_id = setup_run(service, user, connections=[{'connection_id': connection['id'], 'allowed_tools': ['quality_read'], 'auto_approved_tools': []}])
    result = {'structuredContent': {'sources': [{'kind': 'data', 'title': 'Quality lots', 'excerpt': '{"passed":97,"total":100}', 'locator': {'database': 'quality', 'table': 'lots', 'fields': ['passed', 'total'], 'filters': {'line': 'L1', 'api_key': 'never visible'}, 'aggregation': {'numerator': 97, 'denominator': 100}, 'password': 'never visible'}}]}}
    source = service.evidence.register_mcp(run_id, connection['id'], result, tool_name=f"mcp__c{connection['id']}__quality_read")[0]
    assert source['locator']['database'] == 'quality'
    assert source['locator']['missing_fields'] == ['time_range', 'timezone', 'queried_at']
    assert 'never visible' not in json.dumps(source)
    assert source['locator']['aggregation'] == {'numerator': 97, 'denominator': 100}
    service.db.execute('UPDATE connections SET enabled=0 WHERE id=?', (connection['id'],))
    with pytest.raises(AppError):
        service.evidence.get(source['id'], user)


def test_web_source_requires_capability_and_avoids_signed_links(system):
    _, service, users = system
    user = users['owner']
    assistant, _, run_id = setup_run(service, user, web=True)
    source = service.evidence.register(run_id, 'web', 'Public specification', {'url': 'https://example.com/spec', 'fetched_at': now()}, 'Public content', resource_type='web', resource_id=assistant['id'], original_url='https://example.com/spec?api_key=hidden')
    assert source['original_url'] is None
    service.save_assistant({'web_search_enabled': False}, assistant['id'], user=user)
    with pytest.raises(AppError):
        service.evidence.get(source['id'], user)


def test_run_tool_rechecks_kb_binding_and_read_page_bounds(system):
    _, service, users = system
    user = users['owner']
    first = service.knowledge.save({'name': 'Assigned'}, user)
    second = service.knowledge.save({'name': 'Not assigned'}, user)
    one = service.knowledge.upload(first['id'], user, 'one.txt', b'Allowed pressure 7 bar.')
    two = service.knowledge.upload(second['id'], user, 'two.txt', b'Forbidden pressure 9 bar.')
    assistant, _, run_id = setup_run(service, user, [first['id']])
    with pytest.raises(AppError):
        asyncio.run(service.knowledge.read_for_run(user, assistant, run_id, two['id']))
    with pytest.raises(AppError):
        asyncio.run(service.knowledge.read_for_run(user, assistant, run_id, one['id'], page=999))
    service.save_assistant({'knowledge_base_ids': []}, assistant['id'], user=user)
    assert asyncio.run(service.knowledge.search_for_run(user, assistant, run_id, 'pressure'))['results'] == []


def test_running_turn_uses_pinned_knowledge_versions(system):
    _, service, users = system
    user = users['owner']
    kb = service.knowledge.save({'name': 'Versioned'}, user)
    doc = service.knowledge.upload(kb['id'], user, 'limits.md', b'Pressure limit 7 bar.')
    assistant, _, run_id = setup_run(service, user, [kb['id']])
    service.knowledge.snapshot_for_run(run_id, assistant, user)
    service.knowledge.upload(kb['id'], user, 'limits.md', b'Pressure limit 9 bar.', doc['id'])
    result = asyncio.run(service.knowledge.search_for_run(user, assistant, run_id, 'pressure'))
    assert result['results'][0]['version'] == 1
    assert '7 bar' in result['results'][0]['text']
    read = asyncio.run(service.knowledge.read_for_run(user, assistant, run_id, doc['id']))
    assert read['results'][0]['version'] == 1
    with pytest.raises(AppError):
        asyncio.run(service.knowledge.read_for_run(user, assistant, run_id, doc['id'], version=2))


def test_file_sources_require_actual_attachment_in_conversation(system):
    _, service, users = system
    user = users['owner']
    _, conversation, run_id = setup_run(service, user)
    file_id = uid()
    service.db.execute('INSERT INTO files(id,user_id,name,mime_type,size,path,created_at,conversation_id) VALUES(?,?,?,?,?,?,?,?)', (file_id, user['id'], 'report.txt', 'text/plain', 10, 'private-path', now(), conversation['id']))
    source = service.evidence.register(run_id, 'knowledge', 'report.txt', {'file': 'report.txt'}, 'Measured 8 bar', resource_type='file', resource_id=file_id)
    assert source['content_url'] == f'/api/v1/files/{file_id}/content'
    _, _, other_run = setup_run(service, user)
    with pytest.raises(AppError):
        service.evidence.register(other_run, 'knowledge', 'report.txt', {'file': 'report.txt'}, 'Measured 8 bar', resource_type='file', resource_id=file_id)


def test_document_delete_revokes_registered_source_excerpt(system):
    client, service, users = system
    user = users['owner']
    kb = service.knowledge.save({'name': 'Delete test'}, user)
    doc = service.knowledge.upload(kb['id'], user, 'private.txt', b'Confidential obsolete maintenance procedure.')
    assistant, _, run_id = setup_run(service, user, [kb['id']])
    item = asyncio.run(service.knowledge.read_for_run(user, assistant, run_id, doc['id']))['results'][0]
    login(client, 'owner')
    assert client.delete(f"/api/v1/knowledge-documents/{doc['id']}").status_code == 204
    assert client.get(f"/api/v1/sources/{item['source_id']}").status_code == 404
    assert service.evidence.finalize(run_id, item['citation'])['citations'] == []


def test_tool_level_revocation_and_untrusted_tool_metadata(system):
    _, service, users = system
    user = users['owner']
    connection = service.save_connection({'name': 'Tool ACL', 'url': 'http://127.0.0.1:8767/mcp', 'auth_type': 'none'}, user=user)
    service.db.execute('UPDATE connections SET tools=? WHERE id=?', (dump([{'name': 'sensitive_lookup'}, {'name': 'harmless'}]), connection['id']))
    assistant, _, run_id = setup_run(service, user, connections=[{'connection_id': connection['id'], 'allowed_tools': ['sensitive_lookup', 'harmless'], 'auto_approved_tools': []}])
    result = {'sources': [{'kind': 'data', 'title': 'Sensitive', 'excerpt': 'Restricted 73', 'locator': {'tool': 'harmless', 'database': 'finance'}}]}
    source = service.evidence.register_mcp(run_id, connection['id'], result, 'sensitive_lookup')[0]
    assert source['locator']['tool'] == 'sensitive_lookup'
    service.save_assistant({'connections': [{'connection_id': connection['id'], 'allowed_tools': ['harmless'], 'auto_approved_tools': []}]}, assistant['id'], user=user)
    with pytest.raises(AppError):
        service.evidence.get(source['id'], user)


@pytest.mark.parametrize('link', [
    '[9](/api/v1/sources/ID?unregistered=1)', '[9](/api/v1/sources/ID#extra)',
    '[9](https://workspace.example/api/v1/sources/ID?unregistered=1)',
    '[9][ref]\n\n[ref]: /api/v1/sources/ID?other=1',
    'https://workspace.example/api/v1/sources/ID?other=1',
    '[9](/api/v1/%73ources/ID)', '[9](/api/v1/sources%2FID)',
    '[9](/api/v1/dummy/../sources/ID)', '[9](&#47;api/v1/sources/ID)',
    '[9](<%2Fapi%2Fv1%2Fsources%2FID>)',
])
def test_internal_source_url_aliases_cannot_bypass_registry(system, link):
    _, service, users = system
    _, _, run_id = setup_run(service, users['owner'])
    result = service.evidence.finalize(run_id, link)
    assert '/api/v1/sources/' not in result['text']
    assert result['citations'] == []


def test_docx_page_break_preserves_paragraph_without_invented_pdf_page(system):
    _, service, users = system
    user = users['owner']
    kb = service.knowledge.save({'name': 'Docx positioning'}, user)
    content = io.BytesIO()
    with zipfile.ZipFile(content, 'w') as archive:
        archive.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>First logical paragraph.</w:t><w:br w:type="page"/></w:r></w:p><w:p><w:r><w:t>Second page target pressure is 37 bar.</w:t></w:r></w:p></w:body></w:document>')
    doc = service.knowledge.upload(kb['id'], user, 'pages.docx', content.getvalue())
    assistant, _, run_id = setup_run(service, user, [kb['id']])
    item = asyncio.run(service.knowledge.search_for_run(user, assistant, run_id, 'target pressure'))['results'][0]
    source = service.evidence.get(item['source_id'], user)
    assert source['locator']['page'] is None and source['locator']['paragraph'] == 2
    assert all(chunk['page'] is None for chunk in service.knowledge.chunks(doc['id'], user))


@pytest.mark.parametrize('shape', ['structured', 'flat_content', 'blocks', 'string', 'list', 'block', 'nested_content', 'content_dict', 'duplicated'])
def test_actual_sdk_flattened_mcp_result_registers_evidence(system, shape):
    _, service, users = system
    user = users['owner']
    connection = service.save_connection({'name': 'SDK shape', 'url': 'http://127.0.0.1:8767/mcp', 'auth_type': 'none'}, user=user)
    service.db.execute('UPDATE connections SET tools=? WHERE id=?', (dump([{'name': 'read_quality'}]), connection['id']))
    _, _, run_id = setup_run(service, user, connections=[{'connection_id': connection['id'], 'allowed_tools': ['read_quality'], 'auto_approved_tools': []}])
    payload = {'passed': 97, 'total': 100, 'sources': [{'kind': 'data', 'title': 'Inspection', 'excerpt': '97 of 100', 'locator': {'database': 'quality', 'table': 'lots', 'fields': ['passed', 'total']}}]}
    raw = json.dumps(payload)
    block = {'type': 'text', 'text': raw}
    shapes = {'structured': {'structuredContent': payload}, 'flat_content': {'content': raw}, 'blocks': {'content': [block]}, 'string': raw, 'list': [block], 'block': block, 'nested_content': {'content': json.dumps({'content': raw})}, 'content_dict': {'content': payload}, 'duplicated': {'structuredContent': payload, 'content': [block]}}
    sources = service.evidence.register_mcp(run_id, connection['id'], shapes[shape], f"mcp__c{connection['id']}__read_quality")
    assert len(sources) == 1 and sources[0]['excerpt'] == '97 of 100'
    final = service.evidence.finalize(run_id, '合格率97%。[[source:' + sources[0]['id'] + ']]')
    assert len(final['citations']) == 1


@pytest.mark.parametrize('result', [
    {'isError': True, 'content': '{"sources":[{"locator":{}}]}'},
    {'is_error': True, 'structuredContent': {'sources': [{'locator': {}}]}},
    {'content': '{"isError":true,"sources":[{"locator":{}}]}'},
    {'content': 'not JSON'}, {'content': '{malformed'},
    {'content': '{"sources": "not a list"}'},
    {'content': ' ' * 512001 + '{"sources":[]}'},
])
def test_mcp_normalizer_ignores_error_malformed_or_oversized_results(result):
    from app.evidence import mcp_payloads
    assert mcp_payloads(result) == []
