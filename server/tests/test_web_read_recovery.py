"""Host-side web recovery: truthful body evidence, bounded retries, no query egress."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.security import AppError
from app.web_tools import WebTools, fetch_public
from test_web_tools import governed_service
from app.tool_error_details import workspace_web_error


def retry_policy(monkeypatch, attempts=3):
    from app.capability_execution import CapabilityExecutor
    original=CapabilityExecutor.effective
    def effective(self,context,capability,arguments):
        value=original(self,context,capability,arguments)
        return {**value,'max_attempts':attempts} if capability=='fetch' else value
    monkeypatch.setattr(CapabilityExecutor,'effective',effective)


@pytest.mark.parametrize('status',[403,404])
def test_http_access_and_missing_errors_keep_nonretryable_http_status(monkeypatch,status):
    def transport(**kwargs):
        value=httpx.MockTransport(lambda request:httpx.Response(status,content=b'private response body'))
        value._pool=SimpleNamespace(_network_backend=None)
        return value
    monkeypatch.setattr('app.web_tools.httpx.AsyncHTTPTransport',transport)
    with pytest.raises(AppError) as caught:asyncio.run(fetch_public('https://example.com/source'))
    assert caught.value.status==status
    assert caught.value.code=='WEB_FETCH_FAILED'
    assert 'private' not in caught.value.message


@pytest.mark.parametrize('body,code',[
    (b'<html><head><title>Just a moment...</title></head><body><p>Verify you are human</p><script src="/cdn-cgi/challenge-platform/x.js"></script></body></html>','WEB_ACCESS_CHALLENGE'),
    (b'<html><head><title>Article title</title></head><body><div id="root"></div><script src="/app.js"></script></body></html>','WEB_JAVASCRIPT_REQUIRED'),
    (b'<html><title>Just a moment</title><body>Verify you are human.</body></html>','WEB_ACCESS_CHALLENGE'),
])
def test_http_200_challenge_and_javascript_shell_never_register_body(tmp_path,monkeypatch,body,code):
    service,user,assistant,run_id=governed_service(tmp_path)
    monkeypatch.setattr('app.web_tools.fetch_public',AsyncMock(return_value=('https://example.com/source','text/html',body)))
    try:
        with pytest.raises(AppError) as caught:
            asyncio.run(WebTools(service).read_for_run(user,assistant,run_id,'https://example.com/source'))
        assert caught.value.code==code and service.evidence.items==[]
    finally:service.db.close()


def test_repeated_failed_url_new_calls_do_not_repeat_http(tmp_path,monkeypatch):
    service,user,assistant,run_id=governed_service(tmp_path)
    fetch=AsyncMock(side_effect=AppError(504,'WEB_TIMEOUT','private transport detail'))
    monkeypatch.setattr('app.web_tools.fetch_public',fetch)
    async def scenario():
        for i in range(3):
            with pytest.raises(AppError):
                await WebTools(service).read_for_run(user,assistant,run_id,'https://example.com/source#'+str(i),
                                                    query='different locator '+str(i),call_id='call-'+str(i))
    try:
        asyncio.run(scenario())
        assert fetch.await_count==1 and service.evidence.items==[]
    finally:service.db.close()


@pytest.mark.parametrize('body',[
    b'<html><head><title>Pressure</title></head><body><p>7 bar</p><script src="/app.js"></script></body></html>',
    b'<html><head><title>How browser checks work</title></head><body><p>Verify you are human is a message described in this guide.</p><noscript>Please enable JavaScript to continue.</noscript><script src="/cdn-cgi/challenge-platform/example.js"></script></body></html>',
    b'<p>7 bar</p>',
    b'<html><head><title>Pressure</title><body><p>7 bar</p></body></html>',
    b'<html><title>Security verification</title><article>Our UI displays "verify you are human" before login. This article explains the authentication flow.</article></html>',
])
def test_short_real_pages_and_documentation_are_not_challenges(tmp_path,monkeypatch,body):
    service,user,assistant,run_id=governed_service(tmp_path)
    monkeypatch.setattr('app.web_tools.fetch_public',AsyncMock(return_value=('https://example.com/source','text/html',body)))
    try:
        result=asyncio.run(WebTools(service).read_for_run(user,assistant,run_id,'https://example.com/source'))
        assert result['source_id'] and len(service.evidence.items)==1
        assert 'enable JavaScript' not in result['text']
        assert service.evidence.items[0][0][3]['content_type']=='page_text'
    finally:service.db.close()


def test_title_alone_is_not_body(tmp_path,monkeypatch):
    service,user,assistant,run_id=governed_service(tmp_path)
    monkeypatch.setattr('app.web_tools.fetch_public',AsyncMock(return_value=('https://example.com','text/html',b'<html><title>Article title</title><body></body></html>')))
    try:
        with pytest.raises(AppError) as caught:asyncio.run(WebTools(service).read_for_run(user,assistant,run_id,'https://example.com'))
        assert caught.value.code=='WEB_EMPTY' and service.evidence.items==[]
    finally:service.db.close()


@pytest.mark.parametrize('status',[403,404])
def test_policy_never_retries_403_or_404(tmp_path,monkeypatch,status):
    service,user,assistant,run_id=governed_service(tmp_path)
    retry_policy(monkeypatch)
    requests=[]
    def handler(request):
        requests.append(request)
        return httpx.Response(status,content=b'private denied body')
    def transport(**kwargs):
        value=httpx.MockTransport(handler);value._pool=SimpleNamespace(_network_backend=None);return value
    monkeypatch.setattr('app.web_tools.httpx.AsyncHTTPTransport',transport)
    try:
        with pytest.raises(AppError):asyncio.run(WebTools(service).read_for_run(user,assistant,run_id,'https://example.com'))
        assert len(requests)==1 and service.evidence.items==[]
    finally:service.db.close()


def test_transient_policy_gets_only_one_extra_attempt_and_new_call_stays_suppressed(tmp_path,monkeypatch):
    service,user,assistant,run_id=governed_service(tmp_path)
    retry_policy(monkeypatch)
    fetch=AsyncMock(side_effect=AppError(504,'WEB_TIMEOUT','private timeout'))
    monkeypatch.setattr('app.web_tools.fetch_public',fetch)
    async def scenario():
        for identity in ('first','new-call'):
            with pytest.raises(AppError) as caught:
                await WebTools(service).read_for_run(user,assistant,run_id,'https://example.com',call_id=identity)
            details=workspace_web_error('mcp__workspace__web_read',caught.value)
            assert details['failure_kind']=='timeout' and details['retry_suppressed']
    try:
        asyncio.run(scenario())
        assert fetch.await_count==2 and service.evidence.items==[]
    finally:service.db.close()


def test_policy_transient_retry_can_succeed(tmp_path,monkeypatch):
    service,user,assistant,run_id=governed_service(tmp_path)
    retry_policy(monkeypatch)
    fetch=AsyncMock(side_effect=[AppError(504,'WEB_TIMEOUT','private'),('https://example.com','text/plain',b'7 bar')])
    monkeypatch.setattr('app.web_tools.fetch_public',fetch)
    try:
        result=asyncio.run(WebTools(service).read_for_run(user,assistant,run_id,'https://example.com'))
        assert result['text']=='7 bar' and fetch.await_count==2
        assert not service._recent_web_read_failures
    finally:service.db.close()


def test_permission_revocation_is_not_hidden_by_previous_timeout(tmp_path,monkeypatch):
    service,user,assistant,run_id=governed_service(tmp_path)
    fetch=AsyncMock(side_effect=AppError(504,'WEB_TIMEOUT','private'))
    monkeypatch.setattr('app.web_tools.fetch_public',fetch)
    async def scenario():
        with pytest.raises(AppError):await WebTools(service).read_for_run(user,assistant,run_id,'https://example.com')
        service.db.execute('UPDATE assistants SET web_search_enabled=0 WHERE id=?',(assistant['id'],))
        with pytest.raises(AppError) as caught:await WebTools(service).read_for_run(user,assistant,run_id,'https://example.com')
        assert caught.value.code not in {'WEB_TIMEOUT','WEB_RECENT_FAILURE'}
    try:
        asyncio.run(scenario());assert fetch.await_count==1
    finally:service.db.close()


def test_failure_cache_expires_and_has_bounded_capacity(tmp_path,monkeypatch):
    service,user,assistant,run_id=governed_service(tmp_path)
    fetch=AsyncMock(side_effect=AppError(504,'WEB_TIMEOUT','private token'))
    monkeypatch.setattr('app.web_tools.fetch_public',fetch)
    monkeypatch.setattr('app.web_tools.MAX_RECENT_WEB_FAILURES',2)
    monkeypatch.setattr('app.web_tools.WEB_FAILURE_TTL',.01)
    async def scenario():
        for page in ('a','b','c'):
            with pytest.raises(AppError):await WebTools(service).read_for_run(user,assistant,run_id,'https://example.com/'+page+'?secret=value')
        assert len(service._recent_web_read_failures)<=2
        assert 'secret' not in repr(service._recent_web_read_failures) and 'private token' not in repr(service._recent_web_read_failures)
        await asyncio.sleep(.03)
        with pytest.raises(AppError):await WebTools(service).read_for_run(user,assistant,run_id,'https://example.com/c?secret=value')
    try:
        asyncio.run(scenario());assert fetch.await_count==4
    finally:service.db.close()


@pytest.mark.parametrize('code,message,kind,status',[
    ('WEB_FETCH_FAILED','网页返回 HTTP 404，可尝试其他来源','not_found',404),
    ('WEB_FETCH_FAILED','网页返回 HTTP 403，可尝试其他来源','access_denied',403),
    ('WEB_TIMEOUT','private timeout','timeout',None),
    ('WEB_ACCESS_CHALLENGE','private challenge text','access_verification',None),
])
def test_safe_structured_recovery_never_invents_query_or_body(code,message,kind,status):
    value=workspace_web_error('mcp__workspace__web_read',AppError(502,code,message))
    assert value['failure_kind']==kind and value['body_available'] is False
    assert value.get('http_status')==status
    assert value['next_action']['max_alternative_reads']==2
    assert value['next_action']['automatic_search_performed'] is False
    assert 'private' not in json.dumps(value) and 'query' not in value['next_action']


def test_security_denial_does_not_suggest_an_alternative_egress():
    value=workspace_web_error('mcp__workspace__web_read',AppError(422,'WEB_URL_DENIED','private address'))
    assert value['next_action']['max_alternative_reads']==0
    assert value['next_action']['type']=='check_authorization_or_configuration'


def test_harness_style_handler_receives_structured_error_without_sdk_callback(tmp_path,monkeypatch):
    from app.workspace_tools import build_workspace_tools
    service,user,assistant,run_id=governed_service(tmp_path)
    monkeypatch.setattr('app.web_tools.fetch_public',AsyncMock(side_effect=AppError(404,'WEB_FETCH_FAILED','网页返回 HTTP 404，可尝试其他来源')))
    runtime=SimpleNamespace(service=service,conversation_id='conv-test',config={'assistant_snapshot':assistant},
        current_run=service.require('runs',run_id),check_tool=lambda *args:True,interrupted=False)
    try:
        tool=next(item for item in build_workspace_tools(runtime) if item.name=='web_read')
        response=asyncio.run(tool.handler({'url':'https://example.com/missing','query':'must not be sent automatically'}))
        value=json.loads(response['content'][0]['text'])
        assert response['is_error'] and value['failure_kind']=='not_found'
        assert value['next_action']['max_alternative_reads']==2 and service.evidence.items==[]
    finally:service.db.close()
