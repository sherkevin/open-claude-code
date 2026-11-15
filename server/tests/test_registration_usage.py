"""Invitation, account scope, auxiliary-call receipts and local-day boundaries."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from app.db import Database, dump, uid
from app.registration import RegistrationRequest, register, registration_available
from app.security import AppError
from app.usage_accounting import metered_post
from test_api import PASSWORD, client, configured, conversation, login, poll, send

INVITE = "test-invitation-not-a-production-secret"
NEW = {"username": "new-member", "display_name": "New member", "password": "Strong-test-Password-42", "invite_code": INVITE}


def enable(client):
    settings = client.app.state.service.settings
    settings.registration_enabled = True
    settings.registration_invite_code = INVITE
    settings.registration_ip_limit = 20
    settings.registration_global_limit = 50
    return settings


def call_row(service, owner, *, tokens=None, model="test-a", stamp="2026-11-15T01:00:00+00:00", kind="completion", status="succeeded"):
    item = uid()
    service.db.execute("INSERT INTO model_usage_calls VALUES(?,?,?,?,?,?,?,?,?)", (item, owner, kind, model, "公开测试模型 " + model, status, dump(tokens) if tokens is not None else None, stamp, stamp))
    return item


def test_registration_closed_by_default_and_requires_nontrivial_server_invite(client):
    assert client.get('/api/v1/config').json()['registration_enabled'] is False
    assert client.post('/api/v1/auth/register', json=NEW).status_code == 403
    settings = enable(client)
    settings.registration_invite_code = ''
    assert not registration_available(settings)
    assert client.post('/api/v1/auth/register', json=NEW).status_code == 403
    settings.registration_invite_code = 'short'
    assert not registration_available(settings)


def test_invited_member_can_login_but_cannot_escalate_or_read_private_accounts(client):
    enable(client)
    cid = conversation(client)['id']
    admin_id = client.get('/api/v1/auth/me').json()['id']
    private_assistant = client.get('/api/v1/conversations/' + cid).json()['assistant_id']
    assert client.post('/api/v1/auth/register', json={**NEW, 'role':'admin'}).status_code == 422
    assert client.post('/api/v1/auth/register', json={**NEW, 'invite_code':'wrong'}).status_code == 403
    response = client.post('/api/v1/auth/register', json=NEW)
    assert response.status_code == 201, response.text
    assert 'password' not in response.text and INVITE not in response.text
    assert client.get('/api/v1/auth/me').json()['id'] == admin_id  # registration never changes an existing session
    client.post('/api/v1/auth/logout')
    response = client.post('/api/v1/auth/login', json={'username':NEW['username'],'password':NEW['password']})
    assert response.status_code == 200 and response.json()['role'] == 'member'
    assert client.get('/api/v1/users').status_code == 403
    assert client.get('/api/v1/conversations/' + cid).status_code == 404
    assert private_assistant not in [a['id'] for a in client.get('/api/v1/assistants').json()]
    assert client.get('/api/v1/usage?scope=all').status_code == 403
    own = client.get('/api/v1/usage').json()
    assert own['scope'] == 'self' and own['users'] == [] and own['total_runs'] == 0


@pytest.mark.parametrize('password', ['123456789012', 'aaaaaaaaaaaa', 'new-member123456'])
def test_registration_password_policy_does_not_modify_existing_passwords(client, password):
    enable(client)
    assert client.post('/api/v1/auth/register', json={**NEW,'password':password}).status_code == 422
    assert login(client)['role'] == 'admin'


def test_registration_normalizes_account_and_blocks_case_collisions(client):
    enable(client)
    assert client.post('/api/v1/auth/register', json={**NEW,'username':'NEW-member'}).json()['username'] == 'new-member'
    assert client.post('/api/v1/auth/register', json=NEW).status_code == 409
    assert client.post('/api/v1/auth/register', json={**NEW,'username':'ADMIN'}).status_code == 409


def test_failed_invites_consume_persistent_rate_budget_and_forged_ip_cannot_bypass(client):
    settings = enable(client);settings.registration_ip_limit = 2
    for address in ['203.0.113.1','203.0.113.2']:
        assert client.post('/api/v1/auth/register', json={**NEW,'invite_code':'bad'}, headers={'X-Forwarded-For':address}).status_code == 403
    response=client.post('/api/v1/auth/register', json=NEW, headers={'X-Forwarded-For':'203.0.113.3'})
    assert response.status_code == 429
    stored=client.app.state.service.db.all('SELECT * FROM registration_attempts')
    assert len(stored)==2 and len({r['source_hash'] for r in stored})==1
    assert all(INVITE not in str(r) and '203.0.113' not in str(r) for r in stored)
    # New connection/process still sees the rate budget; not an in-memory limiter.
    fresh=Database(settings.data_dir/'app.sqlite3')
    try:
        with pytest.raises(AppError) as error:
            register(SimpleNamespace(settings=settings,db=fresh),RegistrationRequest(**NEW),'testclient')
        assert error.value.status==429
    finally:fresh.close()


def test_usage_explicit_owner_scope_model_and_beijing_day_filters(client):
    service=client.app.state.service;admin=client.get('/api/v1/auth/me').json()
    member=service.create_user({'username':'other','display_name':'Other','password':PASSWORD,'role':'member'})
    fields={'input_tokens':10,'output_tokens':2,'cache_read_input_tokens':0,'cache_creation_input_tokens':1}
    call_row(service,admin['id'],tokens=fields,stamp='2026-11-14T16:00:00+00:00')
    call_row(service,admin['id'],tokens=fields,model='test-b',stamp='2026-11-14T15:59:59+00:00')
    call_row(service,member['id'],tokens=fields,model='private-model')
    own=client.get('/api/v1/usage').json()
    assert own['auxiliary_calls']==2 and own['users']==[]
    assert 'private-model' not in str(own)
    all_users=client.get('/api/v1/usage?scope=all').json()
    assert all_users['auxiliary_calls']==3 and len(all_users['users'])==2
    day=client.get('/api/v1/usage?date_from=2026-11-15&date_to=2026-11-15&model_id=test-a').json()
    assert day['auxiliary_calls']==1 and day['input_tokens']==10 and day['output_tokens']==2
    assert day['timezone']=='Asia/Shanghai' and day['by_day'][0]['day']=='2026-11-15'
    assert day['cache_read_input_tokens']==0 and day['cache_creation_input_tokens']==1
    assert day['model_call_count']==1 and day['estimated_cost_usd'] is None
    login(client,'other')
    assert client.get('/api/v1/usage?scope=all').status_code==403
    assert client.get('/api/v1/usage?model_id=test-a').json()['usage_records']==0
    for query in ['date_from=2026-11-20&date_to=2026-11-01','date_from=2026-02-30','date_to=9999-12-31','scope=owner']:
        assert client.get('/api/v1/usage?'+query).status_code==422


def test_main_ledger_cache_alias_and_empty_receipt_remain_unknown(client):
    cid=conversation(client)['id'];rid=send(client,cid)['run_id'];poll(client,rid)
    service=client.app.state.service
    usage={'token_accounting':{'stages':[{'provider_call':True,'source':'harness_provider'}], 'provider_totals':{'input_tokens':12,'output_tokens':3,'cache_read_input_tokens':7,'cache_write_input_tokens':2}, 'known_subtotals':{'input_tokens':12,'output_tokens':3,'cache_read_input_tokens':7,'cache_write_input_tokens':2}}}
    service.db.execute('UPDATE runs SET usage=? WHERE id=?',(dump(usage),rid))
    value=client.get('/api/v1/usage').json()
    assert value['cache_creation_input_tokens']==2 and value['model_call_count']==1
    usage['token_accounting']['stages']=[]
    service.db.execute('UPDATE runs SET usage=? WHERE id=?',(dump(usage),rid))
    value=client.get('/api/v1/usage').json()
    assert value['input_tokens'] is None and value['model_call_count'] is None


@pytest.mark.parametrize('mode',['success','invalid-json','transport','cancel'])
def test_actual_auxiliary_request_records_usage_even_if_answer_cannot_be_used(client, mode):
    service=client.app.state.service;owner=client.get('/api/v1/auth/me').json()['id']
    class HTTP:
        async def post(self,*args,**kwargs):
            if mode=='transport':raise httpx.ConnectError('synthetic failure')
            if mode=='cancel':raise asyncio.CancelledError()
            return httpx.Response(200,json={'usage':{'input_tokens':9,'output_tokens':1,'cache_read_input_tokens':0,'cache_creation_input_tokens':0}}) if mode=='success' else httpx.Response(200,content=b'not-json')
    async def request():
        try:await metered_post(HTTP(),'https://synthetic.invalid',service=service,kind='completion',selection={'model_id':'public-id','model_label':'Public model'},payload={'private':'never stored'},user_id=owner)
        except (httpx.HTTPError,asyncio.CancelledError):pass
    asyncio.run(request())
    rows=service.db.all('SELECT * FROM model_usage_calls');assert len(rows)==1
    assert 'never stored' not in str(rows) and 'synthetic.invalid' not in str(rows)
    value=client.get('/api/v1/usage').json()
    assert value['model_call_count']==1 and value['estimated_cost_usd'] is None
    assert value['input_tokens']==(9 if mode=='success' else None)
    assert value['output_tokens']==(1 if mode=='success' else None)
    assert rows[0]['status']==('cancelled' if mode=='cancel' else 'failed' if mode=='transport' else 'succeeded')


def test_invitation_is_admin_only_not_in_public_config_and_not_cacheable(client):
    enable(client)
    public=client.get('/api/v1/config')
    assert INVITE not in public.text and public.json()['registration_enabled'] is True
    response=client.get('/api/v1/users/registration-invite')
    assert response.status_code==200 and response.json()['invite_code']==INVITE
    assert 'no-store' in response.headers.get('cache-control','')
    client.app.state.service.create_user({'username':'ordinary','display_name':'Member','password':PASSWORD,'role':'member'})
    login(client,'ordinary')
    assert client.get('/api/v1/users/registration-invite').status_code==403
    client.post('/api/v1/auth/logout')
    assert client.get('/api/v1/users/registration-invite').status_code==401


def test_trial_registration_route_preserves_invitation_guard(configured):
    from app.main import create_app
    from fastapi.testclient import TestClient
    configured.deployment_mode='trial'
    configured.registration_enabled=True
    configured.registration_invite_code=INVITE
    with TestClient(create_app(configured)) as connection:
        assert connection.post('/api/v1/auth/register',json={**NEW,'invite_code':'wrong'}).status_code==403
        response=connection.post('/api/v1/auth/register',json=NEW)
        assert response.status_code==201,response.text
        assert connection.post('/api/v1/auth/login',json={'username':NEW['username'],'password':NEW['password']}).json()['role']=='member'


def test_followup_shared_and_cached_calls_are_counted_once(client, monkeypatch):
    import app.model_followups as module
    from app.model_followups import conversation_followups
    cid=conversation(client)['id'];rid=send(client,cid)['run_id'];poll(client,rid)
    service=client.app.state.service;user=service.db.one("SELECT * FROM users WHERE username='admin'")
    monkeypatch.setattr(module,'_tab_target',lambda _:('public-model',{'model_id':'public-model','model_label':'Public model','upstream_model':'synthetic'},None))
    monkeypatch.setattr(module,'provider_route',lambda _:SimpleNamespace(production_ready=True))
    monkeypatch.setattr(service,'effective_provider',lambda _: {'ANTHROPIC_API_KEY':'synthetic-only','ANTHROPIC_BASE_URL':'https://provider.test/v1'})
    calls=[]
    class HTTP:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):return False
        async def post(self,endpoint,json):
            calls.append(True);await asyncio.sleep(.03)
            return httpx.Response(200,json={'content':[{'text':'{"suggestions":["比较已返回订单金额？"]}'}],'usage':{'input_tokens':17,'output_tokens':3,'cache_read_input_tokens':0,'cache_creation_input_tokens':0}},request=httpx.Request('POST',endpoint))
    monkeypatch.setattr(module.httpx,'AsyncClient',HTTP)
    async def requests():
        results=await asyncio.gather(*(conversation_followups(service,cid,rid,user) for _ in range(3)))
        cached=await conversation_followups(service,cid,rid,user)
        assert cached['router']['cached'] is True and len(results)==3
    asyncio.run(requests())
    rows=service.db.all('SELECT * FROM model_usage_calls')
    assert len(calls)==len(rows)==1 and rows[0]['user_id']==user['id'] and rows[0]['kind']=='followups'
    result=service.usage(user,model_id='public-model')
    assert result['model_call_count']==1 and result['input_tokens']==17 and result['output_tokens']==3
