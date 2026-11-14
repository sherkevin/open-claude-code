import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from app.config import Settings
from app.service import Service
from app.db import dump
from app.harness_runtime import HarnessRuntime
from app.runtime import SessionActor
from app.web_tools import WebTools

@pytest.fixture
def fixture(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENT_KNOWLEDGE_EMBEDDING_MODEL', 'local-lexical-v1')
    s = Service(Settings(data_dir=tmp_path, provider_settings='', model='fixture', runtime_type='harness'))
    u = s.create_user({'username':'reviewer','display_name':'Reviewer','role':'admin','password':'source-review-synthetic-123'})
    a = s.save_assistant({'name':'Review','web_search_enabled':True},user=u)
    c = s.create_conversation({'assistant_id':a['id'],'title':'Review'},u)
    result, _ = s.prepare_run(c['id'],{'text':'Public fixture','file_ids':[],'client_request_id':'review'},u)
    rid=result['run_id']
    s.db.execute("UPDATE runs SET status='running' WHERE id=?",(rid,))
    actor=SessionActor.__new__(SessionActor)
    actor.service,actor.conversation_id=s,c['id']
    actor.current_run_id,actor.cancelled=rid,False
    actor.manager=SimpleNamespace(service=s,approvals={},context_requests={})
    s.manager=actor.manager
    r=HarnessRuntime(s,c['id'],s.runtime_config(c['id']),actor.approve,actor.emit)
    r.current_run=s.require('runs',rid)
    actor.runtime=r
    yield s,u,a,r
    s.db.close()

def source(s,a,r,number=0,kind='search_excerpt'):
    url=f'https://example.com/industrial/{number}'
    return s.evidence.register(r.current_run['id'],'web',f'Fixture {number}',{'url':url,'content_type':kind,'provider':'bing_rss'},'Public synthetic excerpt',resource_type='web',resource_id=a['id'],original_url=url)

def request(r,call='review'):
    return {'schema':'workspace-tool/v1','sessionId':r.session_id,'callId':call,'toolName':'web_search','arguments':{'query':'synthetic industrial'}}

def test_actual_workspace_content_envelope_is_parsed(fixture,monkeypatch):
    s,u,a,r=fixture
    async def bing(self,query):
        return [{'title':'Fixture','url':'https://example.com/public','excerpt':'Public synthetic excerpt'}]
    monkeypatch.setattr(WebTools,'_bing',bing)
    monkeypatch.setattr(WebTools,'_provider_config',lambda self:('bing_rss','','','',True))
    result=asyncio.run(r.bridge.tools['web_search'].handler({'query':'synthetic industrial'}))
    actual=json.loads(result['content'][0]['text'])['results'][0]['source_id']
    assert s.evidence.source_ids_from_tool_result(result)==[actual]

def test_harness_reused_source_is_bound_to_actual_result(fixture):
    s,u,a,r=fixture
    item=source(s,a,r)
    async def handler(args):
        return {'content':[{'type':'text','text':dump({'results':[{'source_id':item['id']}]})}]}
    r.bridge.tools['web_search']=replace(r.bridge.tools['web_search'],handler=handler)
    asyncio.run(r.bridge.invoke(request(r)))
    completed=[e for e in s.db.events(r.current_run['id'],0) if e['type']=='tool.completed'][-1]
    assert completed['data']['source_ids']==[item['id']]

def test_late_page_read_survives_more_than_30_search_sources(fixture):
    s,u,a,r=fixture
    for n in range(31):
        item=source(s,a,r,n)
        s.db.event(r.current_run['id'],'tool.completed',{'tool_name':'mcp__workspace__web_search','success':True,'source_ids':[item['id']]})
    page=source(s,a,r,32,'page_text')
    s.db.event(r.current_run['id'],'tool.completed',{'tool_name':'mcp__workspace__web_read','success':True,'source_ids':[page['id']]})
    final=s.evidence.finalize(r.current_run['id'],'Result without markers')
    assert [c['id'] for c in final['citations']]==[page['id']]

def test_citation_contract_distinguishes_candidate_from_answer_reference(fixture):
    s,u,a,r=fixture
    item=source(s,a,r)
    s.db.event(r.current_run['id'],'tool.completed',{'tool_name':'mcp__workspace__web_search','success':True,'source_ids':[item['id']]})
    final=s.evidence.finalize(r.current_run['id'],'Result without markers')
    citation=final['citations'][0]
    assert citation.get('attribution')=='search_candidate'
    assert 'original_url' not in citation

def test_disabled_user_cannot_auto_attach_sources(fixture):
    s,u,a,r=fixture
    item=source(s,a,r)
    s.db.event(r.current_run['id'],'tool.completed',{'tool_name':'mcp__workspace__web_search','success':True,'source_ids':[item['id']]})
    s.db.execute('UPDATE users SET enabled=0 WHERE id=?',(u['id'],))
    assert s.evidence.finalize(r.current_run['id'],'Result without markers')['citations']==[]
