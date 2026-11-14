"""Independent, synthetic lease-fencing regression contracts.

Actual connector/facade implementations and fixture DBs are used. A forced
expired persisted deadline is intentional; no assertion relies on elapsed I/O.
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path.cwd() / 'server' / 'tests'))
from test_openclaw_connector import env as connector_env, TOOLS, ALLOWED
from test_openclaw_compat import env as compat_env, auth
from app.openclaw_connector import ConnectorError, ConnectorRegistry
from app.openclaw_compat import OpenClawError, OpenClawFacade
from app.openai_compat import NormalizedResult, NormalizedToolCall

PAST = '2001-01-01T00:00:00+00:00'


def test_recovered_connector_never_returns_or_audits_late_success(connector_env):
    env = connector_env
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        class Executor:
            async def call(self, server, tool, arguments, cancelled, request_id):
                entered.set()
                await release.wait()
                return {'ok': True}
        active = ConnectorRegistry(env.service.db, executor=Executor(), lease_seconds=60, timeout_seconds=30)
        observer = ConnectorRegistry(env.service.db, lease_seconds=60)
        active.register_server(env.other, 'independent-lease', TOOLS, ALLOWED)
        task = asyncio.create_task(active.call(env.other, 'read_ticket', {'id': 'synthetic'}))
        await asyncio.wait_for(entered.wait(), 10)
        request_id = next(iter(active._active))
        env.service.db.execute('UPDATE openclaw_connector_calls SET lease_expires_at=? WHERE request_id=?', (PAST, request_id))
        assert observer.recover_interrupted() == 1
        preserved = env.service.db.one('SELECT * FROM openclaw_connector_calls WHERE request_id=?', (request_id,))
        release.set()
        outcome = (await asyncio.gather(task, return_exceptions=True))[0]
        assert isinstance(outcome, ConnectorError), f'Late result escaped fencing: {outcome!r}'
        assert outcome.status == 503
        assert env.service.db.one('SELECT * FROM openclaw_connector_calls WHERE request_id=?', (request_id,)) == preserved
        assert not any(e['action'] == 'tool.succeeded' for e in active.audit_events(env.other))
    asyncio.run(scenario())


@pytest.mark.parametrize('kind', ['tool_result', 'late_error'])
def test_recovered_facade_preserves_terminal_and_rejects_late_outputs(compat_env, kind):
    env = compat_env
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        class Executor:
            async def execute(self, request, context, cancelled):
                entered.set()
                await release.wait()
                if kind == 'late_error':
                    raise OpenClawError(429, 'SYNTHETIC_UPSTREAM', 'Synthetic unavailable')
                return NormalizedResult(tool_calls=(NormalizedToolCall('independent_late_call', 'lookup', {'q':'synthetic'}),), finish_reason='tool_calls')
        active = OpenClawFacade(env.service, executor=Executor(), lease_seconds=60, executor_timeout_seconds=30)
        observer = OpenClawFacade(env.service, lease_seconds=60)
        principal = active.store.authenticate(auth(env.token_a))
        payload = {'model': 'openclaw/default', 'input': 'synthetic lease', 'user':'independent-review'}
        task = asyncio.create_task(active.execute(principal, 'responses', payload, auth(env.token_a)))
        await asyncio.wait_for(entered.wait(), 10)
        run_id = next(iter(active._active))
        env.service.db.execute('UPDATE openclaw_turns SET lease_expires_at=? WHERE run_id=?', (PAST, run_id))
        assert observer.recover_interrupted() == 1
        preserved = env.service.db.one('SELECT * FROM openclaw_turns WHERE run_id=?', (run_id,))
        assert json.loads(preserved['error_json'])['code'] == 'INTERRUPTED'
        release.set()
        outcome = (await asyncio.gather(task, return_exceptions=True))[0]
        assert env.service.db.one('SELECT * FROM openclaw_turns WHERE run_id=?', (run_id,)) == preserved
        assert isinstance(outcome, OpenClawError), f'Late result escaped fencing: {outcome!r}'
        assert outcome.status == 503
        assert not env.service.db.all('SELECT * FROM openclaw_responses WHERE turn_id=?', (preserved['id'],))
        assert not env.service.db.all('SELECT * FROM openclaw_tool_calls WHERE call_id=?', ('independent_late_call',))
        assert not any(e['action'] == 'turn.completed' and e['run_id'] == run_id for e in active.audit_events())
    asyncio.run(scenario())
