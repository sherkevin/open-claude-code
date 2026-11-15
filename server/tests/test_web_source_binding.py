"""Workspace result -> actual runtime hooks -> durable ledger -> final answer.

All network-facing handlers use synthetic public evidence; no provider is called.
"""
import asyncio
import json
from dataclasses import replace

import pytest

from app.config import Settings
from app.harness_runtime import HarnessRuntime
from app.runtime import RunManager, SDKRuntime
from app.service import Service
from app.workspace_tools import build_workspace_tools
from app.web_tools import WebTools


@pytest.fixture
def system(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENT_KNOWLEDGE_EMBEDDING_MODEL', 'local-lexical-v1')
    service = Service(Settings(data_dir=tmp_path, provider_settings='', model='test', runtime_type='claude'))
    service.settings.provider_env = lambda: {'ANTHROPIC_API_KEY': 'synthetic-not-a-real-key'}
    owner = service.create_user({'username': 'sources', 'display_name': 'Sources', 'role': 'admin', 'password': 'synthetic-password-123'})
    kb = service.knowledge.save({'name': 'Synthetic evidence'}, owner)
    service.knowledge.upload(kb['id'], owner, 'manual.txt', b'Pressure is 7 bar.')
    assistant = service.save_assistant({'name': 'Evidence', 'web_search_enabled': True, 'knowledge_base_ids': [kb['id']]}, user=owner)
    cid = service.create_conversation({'assistant_id': assistant['id'], 'title': 'Sources'}, owner)['id']
    result, _ = service.prepare_run(cid, {'text': 'Research pressure', 'file_ids': [], 'client_request_id': 'sources'}, owner)
    service.db.execute("UPDATE runs SET status='running' WHERE id=?", (result['run_id'],))
    run = service.require('runs', result['run_id'])
    yield service, owner, assistant, run
    service.db.close()


def source(system, name='read', content_type='page_text'):
    service, _, assistant, run = system
    return service.evidence.register(run['id'], 'web', name,
        {'url': f'https://example.com/{name}', 'content_type': content_type, 'fetched_at': '2026-11-14T00:00:00Z'},
        f'{name}: synthetic excerpt', resource_type='web', resource_id=assistant['id'], original_url=f'https://example.com/{name}')


def envelope(source_id):
    return {'content': [{'type': 'text', 'text': json.dumps({'results': [{'source_id': source_id}]})}]}


def runtime_for(system, runtime_type):
    service, _, _, run = system
    async def emit(kind, data):
        service.db.event(run['id'], kind, data)
    runtime = runtime_type(service, run['conversation_id'], service.runtime_config(run['conversation_id']), None, emit)
    runtime.current_run = run
    return runtime


def finish(system, text='Answer without any source marker.'):
    service, _, _, run = system
    service.db.execute('UPDATE messages SET text=? WHERE id=?', (text, run['assistant_message_id']))
    RunManager(service).finish(run['id'], 'succeeded')
    message = service.require('messages', run['assistant_message_id'])
    citations = json.loads(message['citations'])
    updated = [event for event in service.db.events(run['id'], 0) if event['type'] == 'message.updated'][-1]
    assert updated['data']['citations'] == citations
    return citations


@pytest.mark.parametrize('shape', ['text_blocks', 'structured', 'flattened', 'ocr_pages'])
def test_sdk_real_post_hook_binds_real_workspace_envelopes(system, monkeypatch, shape):
    service, _, _, run = system
    registered = source(system)
    async def read(*args, **kwargs):
        return {'source_id': registered['id'], 'text': 'Public evidence'}
    monkeypatch.setattr(WebTools, 'read_for_run', read)
    captured = {}
    class Client:
        def __init__(self, options): captured['options'] = options
        async def connect(self): pass
    monkeypatch.setattr('claude_agent_sdk.ClaudeSDKClient', Client)
    async def scenario():
        runtime = runtime_for(system, SDKRuntime)
        await runtime.connect()
        tool = next(tool for tool in build_workspace_tools(runtime) if tool.name == 'web_read')
        result = await tool.handler({'url': 'https://example.com/read'})
        if shape == 'structured': result = {'structuredContent': json.loads(result['content'][0]['text'])}
        if shape == 'flattened': result = {'content': result['content'][0]['text']}
        if shape == 'ocr_pages':
            result = {'content': [{'type': 'text', 'text': json.dumps({
                'status': 'complete', 'pages': [{'page': 1, 'source_id': registered['id']}],
                'source_ids': [registered['id']]})}]}
        hook = captured['options'].hooks['PostToolUse'][0].hooks[0]
        response = await hook({'tool_name': 'mcp__workspace__web_read', 'tool_response': result}, 'read-1', {})
        assert registered['id'] in response['hookSpecificOutput']['additionalContext']
        # Duplicate callbacks must not emit duplicate completion records.
        await hook({'tool_name': 'mcp__workspace__web_read', 'tool_response': result}, 'read-1', {})
    asyncio.run(scenario())
    completed = [event for event in service.db.events(run['id'], 0) if event['type'] == 'tool.completed']
    assert len(completed) == 1 and completed[0]['data']['source_ids'] == [registered['id']]
    assert finish(system)[0]['attribution'] == 'read'


@pytest.mark.parametrize('failure', ['hook_failure', 'result_error', 'cross_run', 'revoked', 'disabled_user'])
def test_sdk_hook_never_attaches_failed_cross_run_or_revoked_source(system, monkeypatch, failure):
    service, _, assistant, run = system
    registered = source(system)
    captured = {}
    class Client:
        def __init__(self, options): captured['options'] = options
        async def connect(self): pass
    monkeypatch.setattr('claude_agent_sdk.ClaudeSDKClient', Client)
    async def scenario():
        runtime = runtime_for(system, SDKRuntime)
        await runtime.connect()
        result = envelope(registered['id'])
        if failure == 'result_error': result['isError'] = True
        if failure == 'disabled_user': service.db.execute('UPDATE users SET enabled=0 WHERE id=?', (system[1]['id'],))
        if failure == 'revoked': service.db.execute('UPDATE assistants SET web_search_enabled=0 WHERE id=?', (assistant['id'],))
        if failure == 'cross_run':
            other = service.create_conversation({'assistant_id': assistant['id'], 'title': 'Other'}, system[1])
            other_run, _ = service.prepare_run(other['id'], {'text': 'Other', 'file_ids': [], 'client_request_id': 'other'}, system[1])
            service.db.execute('UPDATE evidence_sources SET run_id=? WHERE id=?', (other_run['run_id'], registered['id']))
        hook = captured['options'].hooks['PostToolUse'][0].hooks[0]
        await hook({'tool_name': 'mcp__workspace__web_read', 'tool_response': result,
                    'hook_event_name': 'PostToolUseFailure' if failure == 'hook_failure' else 'PostToolUse'}, 'failed', {})
    asyncio.run(scenario())
    assert finish(system) == []


def test_harness_parallel_reused_sources_are_bound_to_exact_result(system, monkeypatch):
    service, _, _, run = system
    old = source(system, 'old')
    runtime = runtime_for(system, HarnessRuntime)
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def read(arguments):
            if arguments['url'].endswith('/old'):
                entered.set()
                await release.wait()
                return envelope(old['id'])
            fresh = source(system, 'fresh')
            release.set()
            return envelope(fresh['id'])
        runtime.bridge.tools['web_read'] = replace(runtime.bridge.tools['web_read'], handler=read)
        def request(call_id, name):
            return {'schema': 'workspace-tool/v1', 'sessionId': runtime.session_id, 'callId': call_id,
                    'toolName': 'web_read', 'arguments': {'url': f'https://example.com/{name}'}}
        task = asyncio.create_task(runtime.bridge.invoke(request('first', 'old')))
        await entered.wait()
        second = await runtime.bridge.invoke(request('second', 'fresh'))
        first = await task
        assert not first.get('is_error') and not second.get('is_error')
        # Reuse the exact same registry ID again; it is still this call's result.
        await runtime.bridge.invoke(request('third', 'old'))
    asyncio.run(scenario())
    completions = [event['data'] for event in service.db.events(run['id'], 0) if event['type'] == 'tool.completed']
    assert len(completions) == 3
    assert completions[1]['source_ids'] == completions[2]['source_ids'] == [old['id']]
    assert old['id'] not in completions[0]['source_ids']
    assert len(finish(system)) == 2


def test_many_events_repeated_and_internal_sources_never_hide_late_web_read(system):
    service, user, assistant, run = system
    internal = asyncio.run(service.knowledge.search_for_run(user, assistant, run['id'], 'Pressure'))['results'][0]['source_id']
    with service.db.transaction():
        for _ in range(600):
            service.db.event(run['id'], 'tool.completed', {'success': True, 'source_ids': [internal] * 40})
        for i in range(40):
            item = source(system, f'candidate-{i}', 'search_excerpt')
            service.db.event(run['id'], 'tool.completed', {'success': True, 'source_ids': [item['id']]})
        late = source(system, 'late-read')
        service.db.event(run['id'], 'tool.completed', {'success': True, 'source_ids': [late['id']]})
    citations = finish(system, f'Internal conclusion [[source:{internal}]]')
    assert [item['id'] for item in citations] == [internal, late['id']]
    assert [item['attribution'] for item in citations] == ['cited', 'read']


def test_partly_cited_web_sources_and_candidates_keep_distinct_attribution(system):
    service, _, _, run = system
    cited = source(system, 'explicit')
    candidate = source(system, 'candidate', 'search_excerpt')
    for item in [cited, candidate]:
        service.db.event(run['id'], 'tool.completed', {'success': True, 'source_ids': [item['id']]})
    citations = finish(system, f'Answer [[source:{cited["id"]}]]')
    assert [item['attribution'] for item in citations] == ['cited', 'search_candidate']


def test_extractor_rejects_errors_unstructured_text_and_bounded_decode(system):
    evidence = system[0].evidence
    item = source(system)
    assert evidence.source_ids_from_tool_result(envelope(item['id'])) == [item['id']]
    assert evidence.source_ids_from_tool_result({'isError': True, **envelope(item['id'])}) == []
    assert evidence.source_ids_from_tool_result({'text': json.dumps({'source_id': item['id']})}) == []
    assert evidence.source_ids_from_tool_result({'content': '[[source:' + item['id'] + ']] https://example.com'}) == []


def test_ocr_page_sources_are_hints_not_unvalidated_citations(system):
    service, owner, _, run = system
    item = source(system)
    result = {'pages': [{'source_id': item['id']}, {'source_id': 'unregistered'},
                        {'source_id': item['id']}, {'source_id': '<invalid>'}]}
    hints = service.evidence.source_ids_from_tool_result(result)
    assert hints == [item['id'], 'unregistered']
    assert [row['id'] for row in service.evidence.trusted_sources(run['id'], hints, owner)] == [item['id']]
    assert service.evidence.source_ids_from_tool_result({'isError': True, **result}) == []


@pytest.mark.parametrize('failure', ['result_error', 'cross_run', 'revoked'])
def test_harness_failure_cross_run_and_revocation_cannot_bind_sources(system, failure):
    service, owner, assistant, run = system
    item = source(system)
    runtime = runtime_for(system, HarnessRuntime)
    async def handler(arguments):
        result = envelope(item['id'])
        if failure == 'result_error': result['isError'] = True
        if failure == 'revoked':
            service.db.execute('UPDATE assistants SET web_search_enabled=0 WHERE id=?', (assistant['id'],))
        if failure == 'cross_run':
            other = service.create_conversation({'assistant_id': assistant['id'], 'title': 'Other'}, owner)
            other_run, _ = service.prepare_run(other['id'], {'text': 'Other', 'file_ids': [], 'client_request_id': 'other'}, owner)
            service.db.execute('UPDATE evidence_sources SET run_id=? WHERE id=?', (other_run['run_id'], item['id']))
        return result
    runtime.bridge.tools['web_read'] = replace(runtime.bridge.tools['web_read'], handler=handler)
    result = asyncio.run(runtime.bridge.invoke({'schema': 'workspace-tool/v1', 'sessionId': runtime.session_id,
        'callId': 'failure', 'toolName': 'web_read', 'arguments': {'url': 'https://example.com/read'}}))
    if failure != 'cross_run': assert result.get('isError') or result.get('is_error')
    events = [event['data'] for event in service.db.events(run['id'], 0) if event['type'] == 'tool.completed']
    assert events[-1]['source_ids'] == []
    assert finish(system) == []


def test_disabled_account_cannot_bind_previously_completed_sources(system):
    service, owner, _, run = system
    item = source(system)
    service.db.event(run['id'], 'tool.completed', {'success': True, 'source_ids': [item['id']]})
    service.db.execute('UPDATE users SET enabled=0 WHERE id=?', (owner['id'],))
    assert service.evidence.finalize(run['id'], f"Claim [[source:{item['id']}]]")['citations'] == []
