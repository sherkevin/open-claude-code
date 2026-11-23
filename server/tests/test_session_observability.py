"""Synthetic owner-scoped receipts: no model or network requests."""
import base64
import json

import pytest

from app.db import dump, uid
from app.harness_usage import HarnessUsageLedger
from app.session_observability import overview, trajectory
from app.token_savings import TokenAccounting
from test_api import PASSWORD, client, configured, conversation, login
from test_harness_usage_capture import frame


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
    assert list(values) == ['session_count', 'request_count', 'compression_count', 'compression_saved_tokens', 'net_saved_tokens',
        'compression_return_usd', 'cache_hit_rate', 'input_tokens', 'cache_read_tokens', 'output_tokens', 'cost_usd']
    assert values['session_count']['value'] == 1
    assert values['input_tokens']['value'] == values['output_tokens']['value'] == 0
    assert values['request_count']['value'] == values['compression_count']['value'] == 0
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
    assert values['input_tokens']['known_value'] == sum(
        value for name, value in [('input', 10), ('read', 80), ('write', 10)] if name != field)
    assert values['cache_hit_rate']['value'] is None
    assert values['cache_hit_rate'].get('known_value') is None
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
    assert metrics(report)['request_count']['known_value'] == 1
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
    assert metrics(report)['input_tokens']['known_value'] == 100
    assert metrics(report)['output_tokens']['known_value'] == 2


def test_overview_subtotals_and_detail_use_the_same_current_http_receipts(env):
    client, service, user, cid = env
    ledger = HarnessUsageLedger('session', 'run')
    ledger.settle([
        frame('complete', phase='finished', usage={'input_tokens': 100, 'cache_read_input_tokens': 0,
            'cache_creation_input_tokens': 0, 'output_tokens': 0, 'reported_prompt_tokens': 100}),
        frame('only-prompt', phase='finished', usage={'reported_prompt_tokens': 200}),
        frame('only-read', phase='finished', usage={'cache_read_input_tokens': 50}),
    ])
    run(service, user, cid, usage={**ledger.receipt(), 'estimated_cost_usd': .04})
    run(service, user, cid)
    response = client.get('/api/v1/conversations/overview')
    assert response.status_code == 200, response.text
    report = response.json()
    expected = {'request_count': 3, 'input_tokens': 350, 'output_tokens': 0,
                'cache_read_tokens': 50, 'cache_hit_rate': 0, 'cost_usd': .04}
    for key, amount in expected.items():
        metric = metrics(report)[key]
        assert metric['value'] is None and metric['precision'] == 'partial'
        assert metric['known_value'] == amount
        assert (metric['observed_count'], metric['total_count']) == (1, 2)
        assert metric.get('known_estimated', False) is (key == 'cost_usd')
    session = report['sessions'][0]
    assert session['total_input_tokens'] is None and session['known_values']['total_input_tokens'] == 350
    assert session['known_values']['output_tokens'] == 0
    detail = client.get(f'/api/v1/conversations/{cid}/trajectory').json()
    assert detail['summary']['request_count'] is None and detail['summary']['known_values'] == {'request_count': 3}
    models = [item['details'] for item in detail['items'] if item['kind'] == 'model']
    recorded = next(model for model in models if model['request_count'] == 3)
    assert recorded['total_input_tokens'] is None and recorded['known_values']['total_input_tokens'] == 350
    assert recorded['known_values']['output_tokens'] == 0
    assert 'known_values' not in next(model for model in models if model['request_count'] is None)


def test_partial_capture_counts_attempts_without_inventing_provider_zero(env):
    _, service, user, cid = env
    ledger = HarnessUsageLedger('session', 'run')
    ledger.observe(frame(usage={'input_tokens': 0, 'output_tokens': 0}))
    run(service, user, cid, usage=ledger.receipt(), status='cancelled')
    cards = metrics(overview(service, user))
    assert cards['request_count']['value'] is None and cards['request_count']['known_value'] == 1
    assert cards['input_tokens']['value'] is None and cards['input_tokens']['known_value'] == 0
    for field in ('output_tokens', 'cache_read_tokens', 'cache_hit_rate', 'cost_usd', 'compression_count'):
        assert cards[field]['precision'] == 'unavailable'
        assert cards[field]['value'] is None and cards[field].get('known_value') is None


def test_legacy_normalized_harness_defaults_are_not_raw_provider_zeroes(env):
    _, service, user, cid = env
    run(service, user, cid, usage={**receipt(input=0, output=0, read=80, write=0), 'source': 'harness_provider'})
    cards = metrics(overview(service, user))
    for field in ('request_count', 'output_tokens', 'cache_hit_rate'):
        assert cards[field]['value'] is None and cards[field].get('known_value') is None
    assert cards['input_tokens']['value'] is None and cards['input_tokens']['known_value'] == 80
    assert cards['cache_read_tokens']['value'] == 80


def test_verified_no_requests_is_a_real_zero_in_mixed_history(env):
    _, service, user, cid = env
    ledger = HarnessUsageLedger('session', 'run')
    ledger.settle([])
    run(service, user, cid, usage=ledger.receipt())
    run(service, user, cid)
    cards = metrics(overview(service, user))
    for field in ('request_count', 'input_tokens', 'output_tokens', 'cache_read_tokens', 'compression_count'):
        assert cards[field]['value'] is None and cards[field]['known_value'] == 0
        assert cards[field]['precision'] == 'partial'
    assert cards['cost_usd'].get('known_value') is None


def test_cache_ratio_weights_complete_pairs_without_combining_unrelated_samples(env):
    _, service, user, cid = env
    run(service, user, cid, usage=receipt(input=10, read=60, write=30))
    run(service, user, cid, usage={'reported_prompt_tokens': 300, 'cache_read_input_tokens': 60})
    run(service, user, cid, usage={'reported_prompt_tokens': 500})
    run(service, user, cid, usage={'cache_read_input_tokens': 400})
    cards = metrics(overview(service, user))
    assert cards['input_tokens']['known_value'] == 1300
    assert cards['cache_read_tokens']['known_value'] == 520
    assert cards['cache_hit_rate']['value'] is None
    assert cards['cache_hit_rate']['known_value'] == .3
    assert cards['cache_hit_rate']['observed_count'] == 2


def test_contradictory_http_prompt_counters_do_not_become_input_or_rate(env):
    _, service, user, cid = env
    ledger = HarnessUsageLedger('session', 'run')
    ledger.settle([frame(phase='finished', usage={'input_tokens': 60, 'cache_read_input_tokens': 50,
        'cache_creation_input_tokens': 0, 'reported_prompt_tokens': 100, 'output_tokens': 1})])
    run(service, user, cid, usage=ledger.receipt())
    cards = metrics(overview(service, user))
    for field in ('input_tokens', 'cache_hit_rate'):
        assert cards[field]['value'] is None and cards[field].get('known_value') is None
    assert cards['cache_read_tokens']['value'] == 50


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
    assert values['compression_count']['value'] == 1 and values['compression_count']['precision'] == 'observed'
    assert values['compression_saved_tokens']['value'] == 800
    assert values['compression_saved_tokens']['precision'] == precision
    assert values['net_saved_tokens']['value'] is values['compression_return_usd']['value'] is None


def compression_cost(*, input=100, output=20, read=0, write=0):
    ledger = TokenAccounting()
    ledger.record('compression', {'input_tokens': input, 'output_tokens': output,
        'cache_read_input_tokens': read, 'cache_write_input_tokens': write}, source='harness_provider')
    return {'token_accounting': ledger.summary(), 'estimated_cost_usd': None, 'source': 'harness_provider'}


@pytest.mark.parametrize('cost,net,break_even', [
    (compression_cost(read=80), 600, 1),
    (compression_cost(input=900), -120, 2),
])
def test_compression_cost_and_single_reuse_estimate_are_distinct_from_actual_savings(env, cost, net, break_even):
    _, service, user, cid = env
    record = compact(source='harness_token_meter', measurement_schema='compaction-measurement/v1', cost_usage=cost)
    run(service, user, cid, compaction=record, usage=receipt(input=99999))
    report = overview(service, user)
    values = metrics(report)
    assert values['net_saved_tokens']['value'] == net
    assert values['net_saved_tokens']['precision'] == 'estimated'
    assert '单次回用' in values['net_saved_tokens']['note']
    assert '账单' in values['net_saved_tokens']['note']
    assert values['compression_return_usd']['value'] is None
    session = report['sessions'][0]
    assert session['compression_cost_tokens'] == 800 - net
    assert session['compression_break_even_reuses'] == break_even
    assert session['net_saved_tokens'] == net


@pytest.mark.parametrize('mutation', ['no_cost', 'missing_cache', 'wrong_stage', 'unknown_history', 'unbound_after'])
def test_compression_cost_coverage_never_uses_answer_usage_or_invents_old_cost(env, mutation):
    _, service, user, cid = env
    record = compact(source='harness_token_meter', measurement_schema='compaction-measurement/v1',
        cost_usage=compression_cost())
    if mutation == 'no_cost': record.pop('cost_usage')
    if mutation == 'missing_cache': record['cost_usage'] = compression_cost(read=None)
    if mutation == 'wrong_stage': record['cost_usage'] = receipt()
    if mutation == 'unknown_history': run(service, user, cid, compaction=compact())
    if mutation == 'unbound_after': record['after']['_binding'] = 'changed'
    run(service, user, cid, compaction=record, usage=receipt())
    report = overview(service, user)
    assert metrics(report)['net_saved_tokens']['value'] is None
    assert metrics(report)['compression_return_usd']['value'] is None


def test_automatic_compressions_are_counted_from_each_commit_and_measured_as_estimates(env):
    _, service, user, cid = env
    automatic = {'status': 'completed', 'trigger': 'pressure', 'measurement_schema': 'harness-estimated/v1',
                 'pre_tokens': 900, 'post_tokens': 700, 'shadowed_tokens': 777777}
    rid = run(service, user, cid, compaction=automatic)
    event(service, rid, 1, 'context.compaction_started', {'trigger': 'auto'})
    event(service, rid, 2, 'context.compaction_result', automatic)
    event(service, rid, 3, 'context.compaction_result', automatic)
    cards = metrics(overview(service, user))
    assert cards['compression_count']['value'] == 2
    assert cards['compression_saved_tokens']['value'] == 400
    assert cards['compression_saved_tokens']['precision'] == 'estimated'
    assert cards['net_saved_tokens']['value'] is None and cards['net_saved_tokens'].get('known_value') is None


def test_compression_subtotals_only_pair_costs_and_differences_within_each_operation(env):
    _, service, user, cid = env
    source = 'harness_token_meter'
    run(service, user, cid, compaction=compact(200, 100, source=source,
        measurement_schema='compaction-measurement/v1', cost_usage=compression_cost()))
    run(service, user, cid, compaction=compact(200, 200))
    cost_only = compact(measurement_schema='compaction-measurement/v1', cost_usage=compression_cost(input=500))
    cost_only.pop('after')
    run(service, user, cid, compaction=cost_only)
    run(service, user, cid, status='interrupted', compaction={'status': 'interrupted', 'boundary_observed': False})
    report = overview(service, user)
    expected = {'compression_count': 3, 'compression_saved_tokens': 100, 'net_saved_tokens': -20}
    for key, amount in expected.items():
        card = metrics(report)[key]
        assert card['value'] is None and card['known_value'] == amount
        assert card['precision'] == 'partial'
        assert card.get('known_estimated', False) is (key != 'compression_count')
    assert metrics(report)['net_saved_tokens']['observed_count'] == 1
    assert metrics(report)['net_saved_tokens']['total_count'] == 3
    assert report['sessions'][0]['known_values']['net_saved_tokens'] == -20


def test_no_op_compaction_and_unverified_completion_are_different(env):
    _, service, user, cid = env
    rid = run(service, user, cid)
    event(service, rid, 1, 'context.compaction_result', {'status': 'not_needed', 'boundary_observed': False})
    assert metrics(overview(service, user))['compression_count']['value'] == 0
    run(service, user, cid, compaction={'status': 'completed'})
    card = metrics(overview(service, user))['compression_count']
    assert card['value'] is None and card['known_value'] == 0 and card['precision'] == 'partial'


def test_a_finished_summary_request_without_a_commit_does_not_prove_zero_compressions(env):
    _, service, user, cid = env
    ledger = HarnessUsageLedger('session', 'run')
    ledger.settle([frame(phase='finished', purpose='compaction')])
    rid = run(service, user, cid, usage=ledger.receipt())
    event(service, rid, 1, 'context.compaction_started', {'trigger': 'auto'})
    cards = metrics(overview(service, user))
    assert cards['request_count']['value'] == 1
    assert cards['compression_count']['value'] is None and cards['compression_count'].get('known_value') is None


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
    assert metrics(response.json())['input_tokens']['value'] == 100
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
    for key, amount in {'request_count': 1, 'input_tokens': 100, 'output_tokens': 2,
                        'compression_count': 1, 'compression_saved_tokens': 800}.items():
        assert metrics(report)[key]['known_value'] == amount


def test_compression_event_limit_reports_only_retained_operations_and_preserves_empty_sessions(env, monkeypatch):
    _, service, user, cid = env
    import app.session_observability as module
    empty = conversation(env[0])['id']
    rid = run(service, user, cid)
    event(service, rid, 1, 'context.compacted', {'trigger': 'auto', 'pre_tokens': 1000})
    event(service, rid, 2, 'context.compacted', {'trigger': 'auto', 'pre_tokens': 500})
    monkeypatch.setattr(module, 'MAX_COMPACTION_EVENTS', 2)
    report = overview(service, user)
    assert report['coverage']['compression_events_truncated'] is False
    assert metrics(report)['compression_count']['value'] == 2
    monkeypatch.setattr(module, 'MAX_COMPACTION_EVENTS', 1)
    report = overview(service, user)
    assert report['coverage']['compression_events_truncated'] is True
    assert metrics(report)['compression_count']['value'] is None
    assert metrics(report)['compression_count']['known_value'] == 1
    assert metrics(report)['compression_saved_tokens'].get('known_value') is None
    assert next(item for item in report['sessions'] if item['id'] == empty)['compression_count'] == 0


def test_no_model_local_product_execution_is_explicit_zero(env):
    _, service, user, cid = env
    rid = run(service, user, cid)
    service.db.execute('UPDATE runs SET execution_meta=? WHERE id=?', (dump({'execution_kind': 'product_metadata'}), rid))
    report = overview(service, user)
    assert metrics(report)['request_count']['value'] == metrics(report)['input_tokens']['value'] == 0
    item = next(item for item in trajectory(service, user, cid)['items'] if item['kind'] == 'model')
    assert item['title'] == '本地处理（无模型调用）'
    assert item['details']['model_id'] is None
