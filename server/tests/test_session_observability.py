"""Synthetic owner-scoped receipts: no model or network requests."""
import base64
import json

import pytest

from app.db import dump, uid
from app.session_observability import overview, trajectory
from app.token_savings import TokenAccounting
from test_api import PASSWORD, client, configured, conversation, login


STAMP = '2026-01-01T00:00:00+00:00'


@pytest.fixture
def env(client):
    service = client.app.state.service
    user = client.get('/api/v1/auth/me').json()
    cid = conversation(client)['id']
    return client, service, user, cid


def receipt(*, input=10, output=2, read=80, write=10, source='harness_provider', cost=None):
    ledger = TokenAccounting()
    ledger.record('answer', {'input_tokens': input, 'output_tokens': output,
        'cache_read_input_tokens': read, 'cache_write_input_tokens': write}, source=source)
    return {'token_accounting': ledger.summary(), 'estimated_cost_usd': cost}


def run(service, user, cid, *, usage=None, compaction=None, status='succeeded', text='公开回答', input_text='公开输入', stamp=STAMP):
    rid, mid, aid = uid(), uid(), uid()
    for identity, role, content in [(mid, 'user', input_text), (aid, 'assistant', text)]:
        service.db.execute('INSERT INTO messages(id,conversation_id,role,text,run_id,created_at) VALUES(?,?,?,?,?,?)',
            (identity, cid, role, content, rid, stamp))
    service.db.execute('INSERT INTO runs(id,conversation_id,user_id,client_request_id,message_id,assistant_message_id,status,usage,created_at,completed_at,compaction,execution_meta,timings) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (rid, cid, user['id'], uid(), mid, aid, status, dump(usage) if usage is not None else None,
         stamp, stamp if status in {'succeeded', 'failed', 'cancelled', 'interrupted'} else None,
         dump(compaction) if compaction is not None else None,
         dump({'selection': {'model_id': 'legacy-model'}}), dump({'total_ms': 123})))
    return rid


def event(service, rid, identity, kind, data, stamp=STAMP):
    service.db.execute('INSERT INTO events VALUES(?,?,?,?,?)', (rid, identity, kind, dump(data), stamp))


def metrics(value):
    return {item['id']: item for item in value['metrics']}


def compact(before=1000, after=200, *, source='native_runtime', **extra):
    def snapshot(value):
        return {'source': source, 'status': 'available' if source == 'native_runtime' else 'partial',
            'precision': 'observed' if source == 'native_runtime' else 'estimated', 'model': 'synthetic', 'total_tokens': value}
    return {'status': 'completed', 'boundary_observed': True, 'before': snapshot(before), 'after': snapshot(after), **extra}


def test_empty_metrics_are_known_empty_but_savings_not_invented(env):
    _, service, user, cid = env
    report = overview(service, user)
    values = metrics(report)
    assert list(values) == ['session_count', 'request_count', 'compression_saved_tokens', 'net_saved_tokens',
        'compression_return_usd', 'cache_hit_rate', 'input_tokens', 'cache_read_tokens', 'output_tokens', 'cost_usd']
    assert values['session_count']['value'] == 1
    assert values['input_tokens']['value'] == values['output_tokens']['value'] == 0
    assert values['request_count']['value'] == 0
    for key in ['compression_saved_tokens', 'net_saved_tokens', 'compression_return_usd', 'cache_hit_rate', 'cost_usd']:
        assert values[key]['value'] is None
    assert report['sessions'][0]['id'] == cid
    assert report['coverage']['scope'] == 'self'


def test_input_total_and_cache_ratio_use_three_disjoint_buckets(env):
    _, service, user, cid = env
    run(service, user, cid, usage=receipt(cost=.001))
    report = overview(service, user)
    values = metrics(report)
    assert values['input_tokens']['value'] == 100
    assert values['cache_read_tokens']['value'] == 80
    assert values['cache_hit_rate']['value'] == .8
    assert values['request_count']['value'] == 1
    assert values['cost_usd']['value'] == .001 and values['cost_usd']['precision'] == 'estimated'
    session = report['sessions'][0]
    assert session['input_tokens'] == 10 and session['total_input_tokens'] == 100
    assert session['cache_write_tokens'] == 10


@pytest.mark.parametrize('field,invalid', [('input', None), ('read', None), ('write', None), ('input', True), ('read', -1), ('write', float('nan'))])
def test_incomplete_or_invalid_input_bucket_keeps_total_and_rate_unknown(env, field, invalid):
    _, service, user, cid = env
    run(service, user, cid, usage=receipt(**{field: invalid}))
    values = metrics(overview(service, user))
    assert values['input_tokens']['value'] is None
    assert values['cache_hit_rate']['value'] is None
    assert values['output_tokens']['value'] == 2


def test_sdk_aggregate_never_becomes_a_provider_call_count_and_auxiliary_not_allocated(env):
    _, service, user, cid = env
    run(service, user, cid, usage=receipt(source='claude_sdk'))
    run(service, user, cid, usage=receipt())
    service.db.execute('INSERT INTO model_usage_calls VALUES(?,?,?,?,?,?,?,?,?)',
        (uid(), user['id'], 'completion', 'legacy-model', '测试', 'succeeded', dump({'input_tokens': 99999}), STAMP, STAMP))
    report = overview(service, user)
    assert metrics(report)['input_tokens']['value'] == 200
    assert metrics(report)['request_count']['value'] is None
    assert metrics(report)['request_count']['precision'] == 'partial'
    assert report['coverage']['known_request_count'] == 1 and report['coverage']['unknown_request_records'] == 1
    assert 'Tab' in report['coverage']['note']


def test_missing_or_zero_initialized_usage_never_invents_provider_tokens(env):
    _, service, user, cid = env
    run(service, user, cid, usage=receipt())
    run(service, user, cid, usage={'token_accounting': TokenAccounting().summary()})
    run(service, user, cid)
    report = overview(service, user)
    assert metrics(report)['input_tokens']['value'] is None
    assert metrics(report)['output_tokens']['value'] is None
    assert metrics(report)['request_count']['value'] is None
    assert report['coverage']['missing_usage_records'] == 1


@pytest.mark.parametrize('source,precision', [('native_runtime', 'observed'), ('harness_token_meter', 'estimated')])
def test_compression_boundary_and_result_are_one_receipt_not_tool_savings(env, source, precision):
    _, service, user, cid = env
    record = compact(source=source)
    rid = run(service, user, cid, usage=receipt(), compaction=record)
    event(service, rid, 1, 'context.compacted', {'pre_tokens': 1000, 'shadowed_tokens': 999999})
    event(service, rid, 2, 'context.compaction_result', record)
    event(service, rid, 3, 'tool.output_trimmed', {'saved_tokens': 999999})
    report = overview(service, user)
    assert report['sessions'][0]['compression_count'] == 1
    values = metrics(report)
    assert values['compression_saved_tokens']['value'] == 800
    assert values['compression_saved_tokens']['precision'] == precision
    assert values['net_saved_tokens']['value'] is values['compression_return_usd']['value'] is None


@pytest.mark.parametrize('mutation', ['missing_after', 'changed_model', 'changed_binding', 'stale', 'unproven', 'boundary_only'])
def test_unverifiable_compression_does_not_claim_savings(env, mutation):
    _, service, user, cid = env
    record = compact()
    if mutation == 'missing_after': record.pop('after')
    if mutation == 'changed_model': record['after']['model'] = 'different'
    if mutation == 'changed_binding': record['after']['_binding'] = 'different'
    if mutation == 'stale': record['after']['stale'] = True
    if mutation == 'unproven': record.pop('boundary_observed')
    if mutation == 'boundary_only': record = None
    rid = run(service, user, cid, compaction=record)
    if mutation == 'boundary_only': event(service, rid, 1, 'context.compacted', {'pre_tokens': 1000})
    assert metrics(overview(service, user))['compression_saved_tokens']['value'] is None


def test_owner_only_even_admin_and_static_route_before_identifier(env):
    client, service, admin, cid = env
    run(service, admin, cid, usage=receipt())
    service.db.execute('UPDATE conversations SET archived=1 WHERE id=?', (cid,))
    service.create_user({'username': 'member', 'display_name': '成员', 'password': PASSWORD, 'role': 'member'})
    login(client, 'member')
    member = client.get('/api/v1/auth/me').json()
    own = conversation(client)['id']
    run(service, member, own, usage=receipt(input=999))
    assert client.get('/api/v1/conversations/' + cid + '/trajectory').status_code == 404
    assert client.get('/api/v1/conversations/overview').json()['sessions'][0]['id'] == own
    login(client)
    response = client.get('/api/v1/conversations/overview')
    assert response.status_code == 200, response.text
    assert [item['id'] for item in response.json()['sessions']] == [cid]
    assert response.json()['sessions'][0]['status'] == 'archived'
    assert client.get('/api/v1/conversations/' + own + '/trajectory').status_code == 404
    client.post('/api/v1/auth/logout')
    assert client.get('/api/v1/conversations/overview').status_code == 401
    assert client.get('/api/v1/conversations/' + cid + '/trajectory').status_code == 401


def test_trajectory_has_only_public_projection_never_reads_native_private_logs(env, monkeypatch):
    _, service, user, cid = env
    rid = run(service, user, cid, usage=receipt(), text='公开最终回答')
    event(service, rid, 1, 'tool.started', {'tool_name': 'web_read', 'tool_call_id': 't1', 'arguments': {'token': 'SECRET_ARG'}, 'thinking': 'SECRET_THINKING'})
    event(service, rid, 2, 'tool.completed', {'tool_name': 'web_read', 'tool_call_id': 't1', 'success': True,
        'duration_ms': 12, 'source_ids': ['SECRET_SOURCE_PATH'], 'response': 'SECRET_TOOL_OUTPUT', 'headers': 'SECRET_HEADERS'})
    event(service, rid, 3, 'model.thinking', {'text': 'SECRET_REASONING'})
    service.db.execute('INSERT INTO raw_events(run_id,payload,created_at) VALUES(?,?,?)', (rid, 'SECRET_RAW_LOG', STAMP))
    queries = []
    original = service.db.all
    def tracked(sql, values=()):
        queries.append(sql)
        return original(sql, values)
    monkeypatch.setattr(service.db, 'all', tracked)
    report = trajectory(service, user, cid)
    serialized = dump(report)
    assert 'SECRET_' not in serialized
    assert not any('raw_events' in sql for sql in queries)
    assert {item['kind'] for item in report['items']} == {'input', 'model', 'tool'}
    assert any(item['text'] == '公开最终回答' for item in report['items'])
    assert report['summary']['tool_count'] == 2 and report['summary']['tool_call_count'] == 1
    assert report['summary']['request_count'] == 1
    tools = [item for item in report['items'] if item['kind'] == 'tool']
    assert tools[-1]['details']['source_count'] == 1
    assert report['coverage']['includes_private_reasoning'] is False


def test_trajectory_truncates_text_and_never_surfaces_unfinished_or_foreign_assistant(env):
    _, service, user, cid = env
    run(service, user, cid, input_text='入' * 5000, text='出' * 5000)
    run(service, user, cid, status='running', text='SECRET_PARTIAL')
    other = conversation(env[0])['id']
    other_run = run(service, user, other, text='SECRET_FOREIGN_CONVERSATION')
    foreign_mid = service.db.one('SELECT assistant_message_id FROM runs WHERE id=?', (other_run,))['assistant_message_id']
    another = run(service, user, cid)
    service.db.execute('UPDATE runs SET assistant_message_id=? WHERE id=?', (foreign_mid, another))
    report = trajectory(service, user, cid)
    assert 'SECRET_' not in dump(report)
    long = [item for item in report['items'] if item['text_truncated']]
    assert len(long) == 2 and all(len(item['text']) == 4000 for item in long)


def test_cursor_pages_are_ordered_without_loss_and_owner_bound(env):
    client, service, user, cid = env
    for _ in range(3):
        rid = run(service, user, cid, usage=receipt())
        event(service, rid, 1, 'tool.started', {'tool_name': 'web_read'})
    whole = trajectory(service, user, cid)
    cursor = None
    seen = []
    while True:
        page = trajectory(service, user, cid, cursor=cursor, limit=2)
        seen.extend(item['id'] for item in page['items'])
        if not page['has_more']: break
        cursor = page['next_cursor']
    assert seen == [item['id'] for item in whole['items']]
    assert len(seen) == len(set(seen)) == 9
    assert whole['summary']['tool_call_count'] is None
    decoded = json.loads(base64.urlsafe_b64decode(cursor + '=' * (-len(cursor) % 4)))
    decoded['user'] = 'different-owner'
    forged = base64.urlsafe_b64encode(dump(decoded).encode()).decode()
    route = f'/api/v1/conversations/{cid}/trajectory'
    for invalid in [forged, 'invalid!', 'a' * 1025]:
        assert client.get(route, params={'cursor': invalid}).status_code == 422
    assert client.get(route, params={'limit': 101}).status_code == 422


def test_limits_report_partial_and_do_not_present_recent_subset_as_total(env, monkeypatch):
    _, service, user, cid = env
    import app.session_observability as module
    run(service, user, cid, usage=receipt(), compaction=compact())
    run(service, user, cid, usage=receipt(), compaction=compact())
    conversation(env[0])
    monkeypatch.setattr(module, 'MAX_RUNS', 1)
    monkeypatch.setattr(module, 'MAX_SESSIONS', 1)
    report = overview(service, user)
    assert report['coverage']['total_runs'] == 2 and report['coverage']['runs_included'] == 1
    assert report['coverage']['runs_truncated'] and report['coverage']['sessions_truncated']
    for key in ['request_count', 'input_tokens', 'output_tokens', 'cost_usd', 'compression_saved_tokens']:
        assert metrics(report)[key]['value'] is None


def test_no_model_local_product_execution_is_explicit_zero(env):
    _, service, user, cid = env
    rid = run(service, user, cid)
    service.db.execute('UPDATE runs SET execution_meta=? WHERE id=?', (dump({'execution_kind': 'product_metadata'}), rid))
    report = overview(service, user)
    assert metrics(report)['request_count']['value'] == metrics(report)['input_tokens']['value'] == 0
    item = next(item for item in trajectory(service, user, cid)['items'] if item['kind'] == 'model')
    assert item['title'] == '本地处理（无模型调用）'
    assert item['details']['model_id'] is None
