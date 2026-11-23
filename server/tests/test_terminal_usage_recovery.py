"""Historical final usage snapshots remain visible when the runs table lacks a copy."""

from app.session_observability import overview, trajectory
from app.usage_accounting import usage_summary
from test_session_observability import client, configured, env, event, metrics, receipt, run


def test_final_receipt_restores_overview_and_detail_without_duplicate_or_writeback(env):
    _, service, user, cid = env
    rid = run(service, user, cid)
    snapshot = receipt(output=7)
    event(service, rid, 1, 'usage.updated', snapshot)
    event(service, rid, 2, 'run.completed', {'status': 'succeeded', 'usage': snapshot})
    existing = run(service, user, cid, usage=receipt(output=3))
    event(service, existing, 1, 'run.completed', {'status': 'succeeded', 'usage': receipt(output=777)})

    cards = metrics(overview(service, user))
    assert cards['request_count']['value'] == 2
    assert cards['output_tokens']['value'] == 10
    assert cards['input_tokens']['value'] == 200
    assert usage_summary(service, user)['output_tokens'] == 10
    details = [item['details'] for item in trajectory(service, user, cid)['items'] if item['kind'] == 'model']
    assert sorted(detail['output_tokens'] for detail in details) == [3, 7]
    assert service.db.one('SELECT usage FROM runs WHERE id=?', (rid,))['usage'] is None


def test_ignore_wrong_run_state_and_unconfirmed_snapshots(env):
    _, service, user, cid = env
    for state, final in [('succeeded', {'status': 'failed', 'usage': receipt()}),
                         ('succeeded', {'status': 'succeeded', 'usage': 'invalid'}),
                         ('running', {'status': 'running', 'usage': receipt()})]:
        rid = run(service, user, cid, status=state)
        event(service, rid, 1, 'usage.updated', receipt())
        event(service, rid, 2, 'run.completed', final)
    rid = run(service, user, cid)
    event(service, rid, 1, 'run.completed', {'status': 'succeeded', 'usage': receipt()})
    event(service, rid, 2, 'run.completed', {})
    service.db.execute('UPDATE events SET data=? WHERE run_id=? AND id=2', ('bad JSON', rid))
    result = overview(service, user)
    assert result['coverage']['known_request_count'] == 0
    assert metrics(result)['output_tokens']['value'] is None


def test_partial_legacy_call_ledger_keeps_measured_fields_from_terminal_receipt(env):
    _, service, user, cid = env
    from app.token_savings import TokenAccounting
    ledger = TokenAccounting()
    ledger.record('answer', {'input_tokens': 27, 'cache_read_input_tokens': 72, 'cache_write_input_tokens': 1,
                             'output_tokens': 9}, source='harness_provider')
    ledger.record('answer', {}, source='harness_provider')
    rid = run(service, user, cid)
    event(service, rid, 1, 'run.completed', {'status': 'succeeded', 'usage': {
        'source': 'harness_provider', 'token_accounting': ledger.summary()}})
    result = metrics(overview(service, user))
    assert result['request_count']['value'] is None
    assert result['legacy_model_records']['value'] == 2
    for key, observed in [('input_tokens',100), ('cache_read_tokens',72), ('output_tokens',9), ('cache_hit_rate',.72)]:
        assert result[key]['value'] is None
        assert result[key]['known_value'] == observed
        assert result[key]['precision'] == 'partial'


def test_no_cost_is_shown_for_unknown_model_pricing_and_free_local_work(env):
    _, service, user, cid = env
    run(service, user, cid, usage={'source': 'product_metadata'})
    run(service, user, cid, usage=receipt(cost=None))
    card = metrics(overview(service, user))['cost_usd']
    assert card['value'] is None
    assert card.get('known_value') is None
    assert card['precision'] == 'unavailable'
