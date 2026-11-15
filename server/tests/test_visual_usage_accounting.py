"""Visual requests must appear in account usage, including unusable answers."""
import asyncio
import io
import json
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from app.config import Settings
from app.service import Service
from app.usage_accounting import metered_post


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENT_KNOWLEDGE_EMBEDDING_MODEL', 'local-lexical-v1')
    catalog = tmp_path / 'models.json'
    catalog.write_text(json.dumps({'default_model_id':'vision', 'models':[{
        'id':'vision', 'label':'Visual fixture', 'upstream_model':'visual-fixture',
        'capabilities':['multimodal'], 'reasoning_levels':{'default':None},
        'provider_env':{'ANTHROPIC_BASE_URL':'https://tokenhub.tencentmaas.com', 'ANTHROPIC_AUTH_TOKEN':'synthetic-only'},
    }]}))
    app = Service(Settings(data_dir=tmp_path/'data', model_catalog=str(catalog), provider_settings='',
        knowledge_vision_enabled=True, knowledge_vision_model_id='vision'))
    user = app.create_user({'username':'visual-owner','display_name':'Owner','password':'synthetic-password-123','role':'admin'})
    yield app, user
    app.db.close()


@pytest.mark.parametrize('answer', ['valid', 'bad-content', 'http-error', 'transport'])
def test_knowledge_visual_call_is_billed_to_uploading_user(service, monkeypatch, answer):
    app, user = service
    kb = app.knowledge.save({'name':'Visual billing'}, user)
    monkeypatch.setattr(app.knowledge.parser, 'ocr_image', lambda _: 'Public synthetic diagram')
    monkeypatch.setattr(app.knowledge.vision, 'ocr_image', lambda _: None)
    monkeypatch.setattr(app.knowledge.vision, 'embed_image', lambda _: None)
    calls=[]
    class HTTP:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def post(self, endpoint, json):
            calls.append(True)
            if answer == 'transport': raise httpx.ConnectError('synthetic')
            usage={'prompt_tokens':23, 'completion_tokens':7, 'prompt_tokens_details':{'cached_tokens':3}}
            return httpx.Response(429 if answer == 'http-error' else 200, json={
                'choices':[{'message':{'content':'{"entities":[],"relations":[]}' if answer == 'valid' else ''}}],
                'usage':usage,
            })
    monkeypatch.setattr('app.openai_visual.httpx.AsyncClient', HTTP)
    raster=io.BytesIO(); Image.new('RGB',(20,10),'green').save(raster,format='PNG')
    app.knowledge.upload(kb['id'],user,'diagram.png',raster.getvalue())
    rows=app.db.all('SELECT * FROM model_usage_calls')
    assert len(calls)==len(rows)==1
    assert rows[0]['user_id']==user['id'] and rows[0]['kind']=='knowledge_vision'
    view=app.usage(user)
    assert view['model_call_count']==1
    assert view['input_tokens']==(None if answer=='transport' else 20)
    assert view['output_tokens']==(None if answer=='transport' else 7)
    assert view['cache_read_input_tokens']==(None if answer=='transport' else 3)
    assert view['cache_creation_input_tokens'] is None
    assert view['estimated_cost_usd'] is None
    if answer!='transport':
        assert json.loads(rows[0]['usage'])['reported_prompt_tokens']==23
    assert all(secret not in str(rows) for secret in ('synthetic-only','base64,','Public synthetic diagram','tokenhub.tencentmaas'))


@pytest.mark.parametrize('tokens,expected', [
    ({'prompt_tokens':12,'completion_tokens':2},(None,2,None)),
    ({'prompt_tokens':True,'completion_tokens':-1,'prompt_tokens_details':{'cached_tokens':'5'}},(None,None,None)),
    ({'prompt_tokens':0,'completion_tokens':0,'prompt_tokens_details':{'cached_tokens':0}},(0,0,0)),
])
def test_openai_receipts_preserve_known_zero_and_unknown_fields(service,tokens,expected):
    app,user=service
    class HTTP:
        async def post(self,*args,**kwargs): return httpx.Response(200,json={'usage':tokens})
    asyncio.run(metered_post(HTTP(),'https://synthetic.invalid',service=app,kind='knowledge_vision',
        selection={'model_id':'vision','model_label':'Visual fixture'},payload={},user_id=user['id'],usage_protocol='openai'))
    view=app.usage(user)
    assert tuple(view[k] for k in ('input_tokens','output_tokens','cache_read_input_tokens'))==expected
    assert view['cache_creation_input_tokens'] is None


@pytest.mark.parametrize('tokens,expected', [
    ({'prompt_tokens':23,'completion_tokens':7,'prompt_tokens_details':{'cached_tokens':3}}, (20,3,None)),
    ({'prompt_tokens':23,'completion_tokens':7,'prompt_tokens_details':{'cached_tokens':3},'cache_creation_input_tokens':2}, (18,3,2)),
    ({'prompt_tokens':23,'prompt_tokens_details':{'cached_tokens':3},'cache_creation_input_tokens':'2'}, (None,3,None)),
    ({'prompt_tokens':2,'prompt_tokens_details':{'cached_tokens':3}}, (None,None,None)),
    ({'prompt_tokens':4,'prompt_tokens_details':{'cached_tokens':3},'cache_creation_input_tokens':2}, (None,None,None)),
])
def test_openai_prompt_total_and_disjoint_input_buckets(service,tokens,expected):
    app,user=service
    class HTTP:
        async def post(self,*args,**kwargs): return httpx.Response(200,json={'usage':tokens})
    asyncio.run(metered_post(HTTP(),'unused',service=app,kind='knowledge_vision',
        selection={'model_id':'vision','model_label':'Visual fixture'},payload={},user_id=user['id'],usage_protocol='openai'))
    stored=json.loads(app.db.one('SELECT usage FROM model_usage_calls')['usage'])
    assert stored['reported_prompt_tokens']==tokens['prompt_tokens']
    view=app.usage(user)
    assert tuple(view[key] for key in ('input_tokens','cache_read_input_tokens','cache_creation_input_tokens'))==expected


def test_cancellation_after_insert_before_http_is_definitely_not_sent(service,monkeypatch):
    import app.usage_accounting as meter
    app,user=service
    original=meter.offload_write
    async def cancel_after_insert(fn,*args):
        await original(fn,*args)
        if args[0].startswith('INSERT'): raise asyncio.CancelledError()
    monkeypatch.setattr(meter,'offload_write',cancel_after_insert)
    calls=[]
    class HTTP:
        async def post(self,*args,**kwargs): calls.append(True)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(metered_post(HTTP(),'unused',service=app,kind='knowledge_vision',
            selection={'model_id':'vision'},payload={},user_id=user['id'],usage_protocol='openai'))
    assert not calls
    assert app.db.one('SELECT status FROM model_usage_calls')['status']=='not_sent'
    view=app.usage(user)
    assert view['model_call_count']==0 and view['unknown_call_records']==0
    assert view['input_tokens']==view['output_tokens']==0


def test_cancellation_inside_http_keeps_one_attempt_and_unknown_tokens(service):
    app,user=service
    calls=[]
    class HTTP:
        async def post(self,*args,**kwargs):
            calls.append(True)
            raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(metered_post(HTTP(),'unused',service=app,kind='knowledge_vision',
            selection={'model_id':'vision'},payload={},user_id=user['id'],usage_protocol='openai'))
    assert calls==[True]
    assert app.db.one('SELECT status FROM model_usage_calls')['status']=='cancelled'
    view=app.usage(user)
    assert view['model_call_count']==1 and view['unknown_call_records']==0
    assert view['input_tokens'] is None and view['estimated_cost_usd'] is None


def test_crash_left_started_receipt_has_unknown_call_count(service):
    from app.db import now,uid
    app,user=service
    app.db.execute('INSERT INTO model_usage_calls VALUES(?,?,?,?,?,?,?,?,?)',
        (uid(),user['id'],'knowledge_vision','vision','Visual fixture','started',None,now(),None))
    view=app.usage(user)
    assert view['model_call_count'] is None
    assert view['known_model_calls']==0 and view['unknown_call_records']==1
    assert view['input_tokens'] is None and view['estimated_cost_usd'] is None
