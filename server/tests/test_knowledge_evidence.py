import asyncio
import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import dump, now, uid
from app.main import create_app
from app.security import AppError
from test_api import FakeRuntime


@pytest.fixture
def system(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_KNOWLEDGE_EMBEDDING_MODEL", "local-lexical-v1")
    app = create_app(Settings(data_dir=tmp_path, provider_settings="", model="test"), runtime_factory=FakeRuntime)
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


def test_user_can_choose_bm25_or_hybrid_and_copy_bounded_rag_context(system, monkeypatch):
    client, service, users = system
    user = users["owner"]
    kb = service.knowledge.save({"name": "Retrieval modes"}, user)
    service.knowledge.upload(kb["id"], user, "bm25.md", "泵站阀门编号 P-101，维护周期为每周一次。".encode())
    service.knowledge.upload(kb["id"], user, "other.md", "仓储盘点周期为每月一次。".encode())

    # BM25 is a usable human-facing fallback even without an optional vector model.
    def vector_unavailable(*args, **kwargs):
        raise RuntimeError("vector model unavailable")
    monkeypatch.setattr(service.knowledge.embeddings, "embed", vector_unavailable)
    bm25 = service.knowledge.search([kb["id"]], user, "P-101", limit=2, mode="bm25", context_chars=64)
    assert bm25["retrieval"]["mode"] == "bm25"
    assert bm25["results"][0]["retrieval_mode"] == "bm25"
    assert bm25["results"][0]["lexical_score"] > 0
    assert len(bm25["context"]["text"]) <= 64
    assert "P-101" in bm25["context"]["text"]

    login(client, "owner")
    response = client.post(f"/api/v1/knowledge-bases/{kb['id']}/search", json={"query": "P-101", "mode": "bm25", "context_chars": 48})
    assert response.status_code == 200
    payload = response.json()
    assert payload["retrieval"]["mode"] == "bm25"
    assert payload["context"]["characters"] <= 48
    assert payload["results"][0]["title"] == "bm25.md"


def test_hybrid_search_keeps_adjacent_formula_for_quality_verification(system):
    _, service, users = system
    user = users["owner"]
    kb = service.knowledge.save({"name": "Quality verification"}, user)
    source = (
        "# 质量统计口径\n\n"
        "先汇总合格数和检验数，再计算加权合格率：\n\n"
        "```text\n"
        "yield_rate = SUM(qualified_count) / SUM(inspected_count)\n"
        "```\n"
    ).encode()
    document = service.knowledge.upload(kb["id"], user, "quality.md", source)
    result = service.knowledge.search([kb["id"]], user, "加权合格率", limit=5)
    assert document["status"] == "ready"
    assert any("SUM(qualified_count) / SUM(inspected_count)" in item["text"] for item in result["results"])
    assert any(item.get("evidence_type") == "adjacent_source_context" for item in result["results"])


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


def _web_source(service, assistant, run_id, *, content_type="page_text", provider="bing_rss"):
    return service.evidence.register(
        run_id, "web", "公开网页证据",
        {"url": "https://example.com/industrial", "fetched_at": now(),
         "content_type": content_type, "provider": provider},
        "公开网页摘录", resource_type="web", resource_id=assistant["id"],
        original_url="https://example.com/industrial",
    )


def test_web_tool_ledger_auto_attaches_external_link_when_model_omits_marker(system):
    _, service, users = system
    assistant, _, run_id = setup_run(service, users["owner"], web=True)
    source = _web_source(service, assistant, run_id)
    service.db.event(run_id, "tool.completed", {"tool_name": "mcp__workspace__web_read", "success": True, "source_ids": [source["id"]]})

    final = service.evidence.finalize(run_id, "这是一个没有显式引用标记的回答。")
    assert [item["id"] for item in final["citations"]] == [source["id"]]
    assert "网页正文" in final["text"]
    assert "https://example.com/industrial" not in final["text"]
    assert f"/api/v1/sources/{source['id']}" in final["text"]


def test_web_and_internal_sources_are_kept_together_with_candidate_label(system):
    _, service, users = system
    user = users["owner"]
    kb = service.knowledge.save({"name": "混合来源"}, user)
    service.knowledge.upload(kb["id"], user, "manual.txt", "内部工艺规定：压力为 7 bar。".encode())
    assistant, _, run_id = setup_run(service, user, [kb["id"]], web=True)
    internal = asyncio.run(service.knowledge.search_for_run(user, assistant, run_id, "压力"))["results"][0]
    web = _web_source(service, assistant, run_id, content_type="search_excerpt")
    service.db.event(run_id, "tool.completed", {"tool_name": "mcp__workspace__web_search", "success": True, "source_ids": [web["id"]]})

    final = service.evidence.finalize(run_id, "内部结论[[source:%s]]，外部搜索未逐页读取。" % internal["source_id"])
    assert {item["id"] for item in final["citations"]} == {internal["source_id"], web["id"]}
    assert "外部检索候选" in final["text"]


def test_no_web_tool_usage_does_not_fabricate_external_citations(system):
    _, service, users = system
    assistant, _, run_id = setup_run(service, users["owner"], web=True)
    # A registered but unconsumed source is not in the run's tool ledger.
    source = _web_source(service, assistant, run_id)
    final = service.evidence.finalize(run_id, "没有网页工具完成事件。")
    assert final["citations"] == []
    assert source["id"] not in final["text"]


def test_cross_run_or_revoked_web_source_is_not_auto_attached(system):
    _, service, users = system
    user = users["owner"]
    assistant, conversation, first_run = setup_run(service, user, web=True)
    source = _web_source(service, assistant, first_run)
    service.db.event(first_run, "tool.completed", {"tool_name": "mcp__workspace__web_read", "success": True, "source_ids": [source["id"]]})
    second_run = uid()
    service.db.execute(
        "INSERT INTO runs(id,conversation_id,user_id,client_request_id,message_id,status,created_at) VALUES(?,?,?,?,?,'running',?)",
        (second_run, conversation["id"], user["id"], uid(), uid(), now()),
    )
    # A forged event carrying another run's ID is rejected by same_run=True.
    service.db.event(second_run, "tool.completed", {"tool_name": "mcp__workspace__web_read", "success": True, "source_ids": [source["id"]]})
    assert service.evidence.finalize(second_run, "跨运行来源不应出现。")["citations"] == []
    service.save_assistant({"web_search_enabled": False}, assistant["id"], user=user)
    assert service.evidence.finalize(first_run, "权限已撤销。 ")["citations"] == []


def test_search_fallback_source_keeps_provider_link_and_candidate_label(system):
    _, service, users = system
    assistant, _, run_id = setup_run(service, users["owner"], web=True)
    source = _web_source(service, assistant, run_id, content_type="search_excerpt", provider="bing_rss")
    service.db.event(run_id, "tool.completed", {"tool_name": "mcp__workspace__web_search", "success": True, "source_ids": [source["id"]]})
    final = service.evidence.finalize(run_id, "搜索摘要足以说明方向，但没有逐页读取。")
    assert final["citations"][0]["id"] == source["id"]
    assert "外部检索候选" in final["text"]
    assert service.evidence.get(source["id"], users["owner"])["original_url"] == "https://example.com/industrial"


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


def test_xlsx_cell_extraction_and_sheet_provenance(system):
    _, service, users = system
    user = users['owner']
    kb = service.knowledge.save({'name': 'XLSX'}, user)
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as archive:
        archive.writestr('xl/sharedStrings.xml', '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>设备</t></si><si><t>压力</t></si></sst>')
        archive.writestr('xl/workbook.xml', '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="质量数据" sheetId="1" r:id="rId1"/></sheets></workbook>')
        archive.writestr('xl/worksheets/sheet1.xml', '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row><row r="2"><c r="A2" t="s"><v>0</v></c><c r="B2"><v>7.2</v></c></row></sheetData></worksheet>')
    document = service.knowledge.upload(kb['id'], user, 'quality.xlsx', data.getvalue())
    assert document['status'] == 'ready' and document['extraction'] == ['xlsx-cells']
    chunk = service.knowledge.chunks(document['id'], user)[0]
    assert '质量数据' in chunk['text'] and '压力' in chunk['text'] and '7.2' in chunk['text']


def test_knowledge_graph_is_versioned_and_source_linked(system):
    client, service, users = system
    user = users['owner']
    kb = service.knowledge.save({'name': 'Graph'}, user)
    doc = service.knowledge.upload(kb['id'], user, 'graph.md', '# 泵站压力\n\n泵站压力上限为 7 bar。'.encode())
    graph = service.knowledge.graph(kb['id'], user)
    labels = {item['label'] for item in graph['nodes']}
    assert graph['versioned'] and {'泵站压力', 'bar'}.issubset(labels)
    assert graph['edges'] and graph['edges'][0]['provenance']['document_id'] == doc['id']
    login(client, 'owner')
    response = client.get(f"/api/v1/knowledge-bases/{kb['id']}/graph?q=泵站")
    assert response.status_code == 200 and response.json()['nodes']
    service.knowledge.upload(kb['id'], user, 'graph.md', '# 泵站压力\n\n泵站压力上限为 9 bar。'.encode(), doc['id'])
    current = service.knowledge.graph(kb['id'], user)
    assert all(item['source']['version'] == 2 for item in current['nodes'])


def test_knowledge_graph_extracts_typed_entities_with_offsets(system):
    _, service, users = system
    user = users['owner']
    kb = service.knowledge.save({'name': 'Typed graph'}, user)
    text = '设备 P-101 使用 ASTM A36 材料，焊接工艺压力=7 bar，合格率 96.5%。'
    document = service.knowledge.upload(kb['id'], user, 'typed.md', text.encode())
    graph = service.knowledge.graph(kb['id'], user)
    by_kind = {item['kind']: item for item in graph['nodes']}
    assert document['status'] == 'ready'
    assert {'equipment', 'standard', 'process', 'metric'} <= set(by_kind)
    # Entity evidence points to an extracted-page span, not an invented page.
    for item in graph['nodes']:
        assert item['source']['document_id'] == document['id']
        assert item['source']['page'] == 1
        assert 0 <= item['source']['start'] < item['source']['end'] <= len(text)
    assert graph['multimodal']['entity_extraction'] == 'ocr_only'


def test_public_osha_fixture_entities_are_source_bound(system):
    _, service, users = system
    user = users['owner']
    kb = service.knowledge.save({'name': 'Public industrial corpus'}, user)
    root = Path(__file__).resolve().parents[2] / 'fixtures' / 'industrial-public-v1'
    document = service.knowledge.upload(kb['id'], user, 'osha-1910-212.md', (root / 'documents/osha-1910-212.md').read_bytes())
    graph = service.knowledge.graph(kb['id'], user)
    labels = {(item['label'], item['kind']) for item in graph['nodes']}
    assert document['status'] == 'ready'
    assert ('osha', 'organization') in labels
    assert ('cfr 1910.212', 'standard') in labels
    assert ('铣床', 'equipment') in labels
    assert all(item['source']['document_id'] == document['id'] for item in graph['nodes'])


def test_image_upload_reports_local_ocr_requirement(system, monkeypatch):
    _, service, users = system
    user = users['owner']
    kb = service.knowledge.save({'name': 'Images'}, user)
    def missing_ocr(*args):
        raise AppError(422, 'OCR_REQUIRED', 'OCR missing')
    monkeypatch.setattr(service.knowledge.parser, 'ocr_image', missing_ocr)
    image = service.knowledge.upload(kb['id'], user, 'nameplate.png', b'not-an-image')
    assert image['status'] == 'needs_ocr' and image['chunk_count'] == 0


def test_textless_image_can_be_ready_from_visual_index(system, monkeypatch):
    _, service, users = system
    user = users['owner']
    kb = service.knowledge.save({'name': 'Textless images'}, user)
    from PIL import Image
    image_bytes = io.BytesIO()
    Image.new('RGB', (320, 240), '#6b8e23').save(image_bytes, format='PNG')
    monkeypatch.setattr(service.knowledge.parser, 'ocr_image', lambda _path: '')
    monkeypatch.setattr(service.knowledge.vision, 'ocr_image', lambda _path: None)
    monkeypatch.setattr(service.knowledge.vision, 'embed_image', lambda _path: {'model': 'test-clip', 'dimensions': 2, 'vector': [1.0, 0.0], 'device': 'cpu'})
    monkeypatch.setattr(service.knowledge.vision, 'embed_text', lambda _text: {'model': 'test-clip', 'dimensions': 2, 'vector': [1.0, 0.0], 'device': 'cpu'})
    document = service.knowledge.upload(kb['id'], user, 'photo.png', image_bytes.getvalue())
    assert document['status'] == 'ready' and document['chunk_count'] == 0
    assert document['image']['visual_index_status'] == 'ready'
    result = service.knowledge.search([kb['id']], user, '绿色设备', mode='vector')
    assert result['results'][0]['retrieval_mode'] == 'visual'
    assert result['results'][0]['media']['kind'] == 'image'


def test_image_asset_thumbnail_is_local_and_visual_index_is_truthful(system, monkeypatch):
    client, service, users = system
    user = users['owner']
    kb = service.knowledge.save({'name': 'Image assets'}, user)
    from PIL import Image
    image_bytes = io.BytesIO()
    Image.new('RGB', (1200, 600), '#dbead4').save(image_bytes, format='PNG')

    def missing_ocr(*args):
        raise AppError(422, 'OCR_REQUIRED', 'OCR missing')

    monkeypatch.setattr(service.knowledge.parser, 'ocr_image', missing_ocr)
    document = service.knowledge.upload(kb['id'], user, 'line-layout.png', image_bytes.getvalue())
    assert document['status'] == 'needs_ocr'
    assert document['image']['width'] == 1200 and document['image']['height'] == 600
    assert document['image']['thumbnail_width'] <= 640 and document['image']['thumbnail_height'] <= 480
    assert document['image']['visual_index_status'] == 'needs_model'
    assert document['image']['evidence_boundary']
    login(client, 'owner')
    metadata = client.get(f"/api/v1/knowledge-documents/{document['id']}/image").json()
    assert metadata['entity_index_status'] == 'needs_model'
    assert metadata['region_count'] == 4 and metadata['grid'] == {'rows': 2, 'columns': 2, 'max_regions': 16}
    assert all(region['visual_index_status'] == 'needs_model' and region['entity_index_status'] == 'needs_model' for region in metadata['regions'])
    regions = client.get(f"/api/v1/knowledge-documents/{document['id']}/regions").json()
    assert regions['version'] == document['version'] and len(regions['regions']) == 4
    first_region = regions['regions'][0]
    assert first_region['x'] == 0 and first_region['y'] == 0
    region_thumbnail = client.get(first_region['thumbnail_url'])
    assert region_thumbnail.status_code == 200 and region_thumbnail.headers['content-type'].startswith('image/jpeg')
    thumbnail = client.get(metadata['thumbnail_url'])
    assert thumbnail.status_code == 200 and thumbnail.headers['content-type'].startswith('image/jpeg')

    # Region identifiers are bound to immutable document versions; a region
    # from v1 cannot be replayed against the current v2 endpoint.
    service.knowledge.upload(kb['id'], user, 'line-layout.png', image_bytes.getvalue(), document['id'])
    assert client.get(f"/api/v1/knowledge-documents/{document['id']}/regions/{first_region['id']}/thumbnail?version=2").status_code == 404

    login(client, 'peer')
    assert client.get(f"/api/v1/knowledge-documents/{document['id']}/regions").status_code == 404
    assert client.get(first_region['thumbnail_url']).status_code == 404


def test_image_retrieval_attaches_version_bound_media_evidence(system, monkeypatch):
    client, service, users = system
    user = users['owner']
    kb = service.knowledge.save({'name': 'Image retrieval'}, user)
    from PIL import Image
    image_bytes = io.BytesIO()
    Image.new('RGB', (1200, 600), '#dbead4').save(image_bytes, format='PNG')
    monkeypatch.setattr(service.knowledge.parser, 'ocr_image', lambda path: '设备铭牌 P-77 压力 8 bar')
    document = service.knowledge.upload(kb['id'], user, 'nameplate.png', image_bytes.getvalue())
    assert document['status'] == 'ready'

    result = service.knowledge.search([kb['id']], user, 'P-77', mode='bm25')
    hit = result['results'][0]
    media = hit['media']
    assert media['kind'] == 'image'
    assert media['document_id'] == document['id'] and media['version'] == 1
    assert media['sha256'] == document['sha256']
    assert media['visual_index_status'] in {'ready', 'needs_model'}
    assert len(media['regions']) == 4
    assert media['evidence_boundary']
    login(client, 'owner')
    assert client.get(media['thumbnail_url']).status_code == 200
    assert client.get(media['regions'][0]['thumbnail_url']).status_code == 200


def test_visual_only_hits_register_citations_and_honor_run_snapshot(system, monkeypatch):
    _, service, users = system
    user = users['owner']
    kb = service.knowledge.save({'name': 'Visual run evidence'}, user)
    from PIL import Image
    image = io.BytesIO()
    Image.new('RGB', (320, 240), '#6b8e23').save(image, format='PNG')
    monkeypatch.setattr(service.knowledge.parser, 'ocr_image', lambda _path: '')
    monkeypatch.setattr(service.knowledge.vision, 'ocr_image', lambda _path: None)
    monkeypatch.setattr(service.knowledge.vision, 'embed_image', lambda _path: {'model': 'test-clip', 'dimensions': 2, 'vector': [1.0, 0.0], 'device': 'cpu'})
    monkeypatch.setattr(service.knowledge.vision, 'embed_text', lambda _text: {'model': 'test-clip', 'dimensions': 2, 'vector': [1.0, 0.0], 'device': 'cpu'})
    first = service.knowledge.upload(kb['id'], user, 'machine.png', image.getvalue())
    assistant, _, run_id = setup_run(service, user, [kb['id']])
    service.knowledge.snapshot_for_run(run_id, assistant, user)
    second = service.knowledge.upload(kb['id'], user, 'machine.png', image.getvalue(), first['id'])

    result = asyncio.run(service.knowledge.search_for_run(user, assistant, run_id, '绿色设备'))
    assert result['results'] and result['results'][0]['retrieval_mode'] == 'visual'
    hit = result['results'][0]
    assert hit['version'] == 1 and hit['knowledge_base'] == 'Visual run evidence'
    assert hit['citation'].startswith('[[source:')
    source_id = hit['source_id']
    source = service.evidence.get(source_id, user)
    assert source['document_version'] == 1
    assert source['locator']['sha256'] == first['sha256']
    assert '图片视觉召回' in source['excerpt']
    assert second['version'] == 2


def test_explicit_tokenhub_visual_stage_persists_bounded_candidates(tmp_path, monkeypatch):
    catalog = tmp_path / "models.json"
    catalog.write_text(json.dumps({"default_model_id": "vision", "models": [
        {"id": "vision", "label": "Vision", "upstream_model": "provider/vision", "capabilities": ["multimodal"], "reasoning_levels": {"default": None}},
    ]}), encoding="utf-8")
    provider = tmp_path / "provider.json"
    provider.write_text(json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": "test-only-token", "ANTHROPIC_BASE_URL": "http://provider.test/v1"}}), encoding="utf-8")
    from PIL import Image
    image = io.BytesIO()
    Image.new("RGB", (320, 240), "#dbead4").save(image, format="PNG")

    async def fake_visual(**_kwargs):
        return {"json": {"entities": [
            {"label": "Control Server", "kind": "equipment", "confidence": 0.91},
            {"label": "HMI", "kind": "equipment", "confidence": 0.88},
        ], "relations": [{"source": "Control Server", "target": "HMI", "relation": "connects", "confidence": 0.82}]}, "usage": {"total_tokens": 12}}

    monkeypatch.setattr("app.knowledge.call_visual_json", fake_visual)
    app = create_app(Settings(data_dir=tmp_path / "data", model_catalog=str(catalog), provider_settings=str(provider), model="vision", knowledge_vision_model_id="vision", knowledge_vision_enabled=True))
    with TestClient(app):
        service = app.state.service
        user = service.create_user({"username": "owner", "display_name": "Owner", "password": "test-password-123", "role": "admin"})
        kb = service.knowledge.save({"name": "Provider visual"}, user)
        # Keep this test independent from heavyweight local OCR/CLIP weights;
        # the provider stage is the behavior under test.
        monkeypatch.setattr(service.knowledge.parser, "ocr_image", lambda _path: "diagram Control Server HMI")
        monkeypatch.setattr(service.knowledge.vision, "ocr_image", lambda _path: None)
        monkeypatch.setattr(service.knowledge.vision, "embed_image", lambda _path: None)
        document = service.knowledge.upload(kb["id"], user, "diagram.png", image.getvalue())
        assert document["status"] == "ready"
        rows = service.db.all("SELECT label,kind,confidence,source_text FROM knowledge_visual_entities WHERE document_id=?", (document["id"],))
        assert {(row["label"], row["kind"]) for row in rows} == {("Control Server", "model_entity:equipment"), ("HMI", "model_entity:equipment")}
        relation = service.db.one("SELECT relation,confidence,evidence FROM knowledge_visual_relations WHERE document_id=? AND relation='connects'", (document["id"],))
        assert relation and relation["evidence"] == "provider_visual_candidate" and relation["confidence"] == 0.82
        metadata = service.db.one("SELECT metadata_json FROM knowledge_image_assets WHERE document_id=?", (document["id"],))
        assert json.loads(metadata["metadata_json"])["provider_visual"]["status"] == "ready"


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


def test_web_source_original_url_endpoint_rechecks_revocation(system):
    client, service, users = system
    assistant, _, run_id = setup_run(service, users['owner'], web=True)
    source = _web_source(service, assistant, run_id)
    login(client, 'owner')
    first = client.get(source['url'])
    assert first.status_code == 200
    assert first.json()['original_url'] == 'https://example.com/industrial'
    service.save_assistant({'web_search_enabled': False}, assistant['id'], user=users['owner'])
    denied = client.get(source['url'])
    assert denied.status_code == 404
    assert 'original_url' not in denied.text
