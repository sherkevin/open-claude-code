"""Isolated history fixtures only. No live Orca or Feishu input is sent."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock

try:
    from .orca_transport import OrcaTransport, TransportError
except ImportError:
    from orca_transport import OrcaTransport, TransportError


SID = '12345678-1234-1234-1234-123456789abc'


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.history = self.home / 'thread_history_1.sqlite'
        with sqlite3.connect(self.history) as db:
            db.executescript('''
                CREATE TABLE thread_items (thread_id TEXT,turn_id TEXT,item_id TEXT,
                    rollout_ordinal INTEGER,item_type TEXT,item_json TEXT);
                CREATE TABLE thread_turns (thread_id TEXT,turn_id TEXT,rollout_ordinal INTEGER,
                    status TEXT,error_json TEXT,first_user_item_id TEXT,
                    final_agent_item_id TEXT,completed_at INTEGER);
            ''')
        self.transport = OrcaTransport(dict(session_id=SID, codex_home=str(self.home),
            cwd=str(self.home), worktree_id='repo::path', pane_key='tab:pane',
            codex_binary='/not/a/live/binary', orca_command=['/not/a/live/orca'],
            state_dir=str(self.home / 'state')))
        self.target = dict(pid=123, starttime=100, handle='term-fixture', incarnation='inc-1',
                           pane_key='tab:pane', session_id=SID)
        self.transport._target = Mock(return_value=self.target)
        self.transport._orca = Mock(return_value={'send': {'accepted': True, 'prompt': {
            'requestId': 'orca-receipt-1', 'stages': ['input_accepted'],
            'provider': 'codex', 'observation': 'supported'}}})

    def user(self, turn, item, text, ordinal, sid=SID):
        self.item(turn, item, 'userMessage', {'type': 'userMessage', 'id': item,
                    'content': [{'type': 'text', 'text': text}]}, ordinal, sid)

    def item(self, turn, item, kind, value, ordinal, sid=SID):
        with sqlite3.connect(self.history) as db:
            db.execute('INSERT INTO thread_items VALUES (?,?,?,?,?,?)',
                       (sid, turn, item, ordinal, kind, json.dumps(value)))

    def finish(self, turn='group-turn', user='group-user', text='GROUP_REPLY', ordinal=2,
               status='completed', sid=SID, phase='final_answer'):
        final_id = turn + '-final'
        with sqlite3.connect(self.history) as db:
            db.execute('INSERT INTO thread_turns VALUES (?,?,?,?,?,?,?,?)',
                       (sid, turn, ordinal, status, None, user, final_id, 100 if status == 'completed' else None))
        self.item(turn, final_id, 'agentMessage',
                  {'type': 'agentMessage', 'id': final_id, 'phase': phase, 'text': text}, ordinal, sid)

    def submit(self):
        return self.transport.submit('event-1', 'Reply to the group task')

    def group_user(self, turn='group-turn', ordinal=1, item='group-user'):
        self.user(turn, item, self.transport._payload('event-1', 'Reply to the group task'), ordinal)

    def test_exact_turn_selected_despite_newer_private_final(self):
        self.assertEqual(self.submit()['status'], 'submitted')
        self.group_user()
        self.finish()
        self.user('private-turn', 'private-user', 'A private conversation', 3)
        self.finish('private-turn', 'private-user', 'PRIVATE_REPLY', 4)
        result = self.transport.poll('event-1')
        self.assertEqual(result['final'], 'GROUP_REPLY')
        self.assertEqual(result['turn_id'], 'group-turn')

    def test_latest_private_final_is_never_fallback(self):
        self.submit()
        self.user('private', 'p-user', 'Private text', 1)
        self.finish('private', 'p-user', 'PRIVATE_REPLY', 2)
        result = self.transport.poll('event-1')
        self.assertNotIn('final', result)
        self.assertEqual(result['status'], 'submitted')

    def test_no_unmarked_historical_result(self):
        self.group_user(ordinal=1)
        self.finish()
        self.submit()
        self.assertNotIn('final', self.transport.poll('event-1'))

    def test_steering_by_direct_user_suppresses_final(self):
        self.submit()
        self.group_user()
        self.user('group-turn', 'direct-steer', 'Private steering', 2)
        self.finish(ordinal=3)
        result = self.transport.poll('event-1')
        self.assertEqual(result['reason'], 'mixed_user_input')
        self.assertNotIn('final', result)
        self.assertNotIn('final', self.transport.poll('event-1'))

    def test_group_input_steering_a_private_turn_is_suppressed(self):
        self.user('mixed-turn', 'direct-first', 'Private task', 1)
        self.submit()
        self.group_user(turn='mixed-turn', ordinal=2)
        self.finish('mixed-turn', 'direct-first', 'COMBINED', 3)
        self.assertEqual(self.transport.poll('event-1')['reason'], 'mixed_user_input')

    def test_marker_substring_or_modified_payload_does_not_match(self):
        self.submit()
        payload = self.transport._payload('event-1', 'Reply to the group task')
        self.user('private', 'p', 'User quoted: ' + payload, 1)
        self.finish('private', 'p', 'PRIVATE', 2)
        self.assertNotIn('final', self.transport.poll('event-1'))

    def test_repeated_identical_submit_does_not_send_twice(self):
        self.submit()
        self.submit()
        self.assertEqual(self.transport._orca.call_count, 1)
        result = self.transport.submit('event-1', 'Changed payload')
        self.assertEqual(result['reason'], 'request_payload_conflict')
        self.assertEqual(self.transport._orca.call_count, 1)

    def test_ambiguous_submission_not_resent_but_history_can_resolve(self):
        self.transport._orca.side_effect = TransportError('orca_request_not_verified')
        self.assertEqual(self.submit()['status'], 'uncertain')
        self.assertEqual(self.submit()['status'], 'uncertain')
        self.assertEqual(self.transport._orca.call_count, 1)
        self.group_user()
        self.finish()
        self.assertEqual(self.transport.poll('event-1')['final'], 'GROUP_REPLY')

    def test_busy_and_changed_targets_do_not_send(self):
        self.transport._target.return_value = None
        self.assertEqual(self.submit()['status'], 'queued')
        self.assertIsNone(self.transport._lookup('event-1'))
        self.transport._target.side_effect = [self.target, {**self.target, 'incarnation': 'inc-2'}]
        self.assertEqual(self.submit()['status'], 'queued')
        self.transport._orca.assert_not_called()

    def test_unsupported_receipt_is_uncertain(self):
        self.transport._orca.return_value = {'send': {'accepted': True}}
        self.assertEqual(self.submit()['status'], 'uncertain')

    def test_unknown_after_bridge_restart_stays_uncertain(self):
        self.assertEqual(self.transport.poll('never-submitted')['status'], 'uncertain')
        self.transport._orca.assert_not_called()

    def test_duplicate_request_in_different_turns_is_suppressed(self):
        self.submit()
        self.group_user()
        self.finish()
        self.group_user('duplicate-turn', 3, 'duplicate-user')
        self.finish('duplicate-turn', 'duplicate-user', ordinal=4)
        result = self.transport.poll('event-1')
        self.assertEqual(result['reason'], 'ambiguous_request_turn')
        self.assertNotIn('final', result)

    def test_other_session_cannot_supply_result(self):
        self.submit()
        other = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
        self.user('other-turn', 'other-user', self.transport._payload('event-1', 'Reply to the group task'), 1, other)
        self.finish('other-turn', 'other-user', 'OTHER_SESSION', 2, sid=other)
        self.assertNotIn('final', self.transport.poll('event-1'))

    def test_failed_turn_and_commentary_are_never_final(self):
        self.submit()
        self.group_user()
        self.finish(status='failed')
        result = self.transport.poll('event-1')
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('final', result)
        with sqlite3.connect(self.history) as db:
            db.execute("UPDATE thread_turns SET status='completed',completed_at=100")
            db.execute('UPDATE thread_items SET item_json=? WHERE item_type=?',
                       (json.dumps({'type': 'agentMessage', 'id': 'group-turn-final',
                                   'phase': 'commentary', 'text': 'private thinking'}), 'agentMessage'))
        self.assertNotIn('final', self.transport.poll('event-1'))

    def test_invalid_id_and_control_bytes_never_send(self):
        for request, prompt in [('bad\nid', 'task'), ('x', 'task\x1b[31m')]:
            self.assertEqual(self.transport.submit(request, prompt)['status'], 'failed')
        self.transport._orca.assert_not_called()

    def test_only_empty_live_composer_is_accepted(self):
        empty = {'terminal': {'tail': ['old transcript', '» Ask Codex to do anything', '? for shortcuts']}}
        self.assertTrue(self.transport._empty_composer(empty))
        self.assertFalse(self.transport._empty_composer({'terminal': {'tail': ['» draft text', '? for shortcuts']}}))
        self.assertFalse(self.transport._empty_composer({'terminal': {'tail': ['» Ask Codex to do anything', 'permission menu']}}))

    def test_native_running_turn_queue_and_goal_all_prevent_dispatch(self):
        with sqlite3.connect(self.home / 'queue_1.sqlite') as db:
            db.execute('CREATE TABLE queued_items (thread_id TEXT)')
        with sqlite3.connect(self.home / 'goals_1.sqlite') as db:
            db.execute('CREATE TABLE thread_goals (thread_id TEXT,status TEXT)')
        self.finish(status='inProgress')
        self.assertFalse(self.transport._native_idle())
        with sqlite3.connect(self.history) as db:
            db.execute("UPDATE thread_turns SET status='completed'")
        self.assertTrue(self.transport._native_idle())
        with sqlite3.connect(self.home / 'queue_1.sqlite') as db:
            db.execute('INSERT INTO queued_items VALUES (?)', (SID,))
        self.assertFalse(self.transport._native_idle())
        with sqlite3.connect(self.home / 'queue_1.sqlite') as db:
            db.execute('DELETE FROM queued_items')
        with sqlite3.connect(self.home / 'goals_1.sqlite') as db:
            db.execute('INSERT INTO thread_goals VALUES (?,?)', (SID, 'active'))
        self.assertFalse(self.transport._native_idle())

    def idle_native_with_goal(self, status):
        with sqlite3.connect(self.home / 'queue_1.sqlite') as db:
            db.execute('CREATE TABLE queued_items (thread_id TEXT)')
        with sqlite3.connect(self.home / 'goals_1.sqlite') as db:
            db.execute('CREATE TABLE thread_goals (thread_id TEXT,status TEXT)')
            db.execute('INSERT INTO thread_goals VALUES (?,?)', (SID, status))
        self.finish()

    def test_blocked_and_paused_goals_do_not_reserve_idle_session(self):
        self.idle_native_with_goal('blocked')
        for status in ('blocked', 'paused', 'completed'):
            with self.subTest(status=status):
                with sqlite3.connect(self.home / 'goals_1.sqlite') as db:
                    db.execute('UPDATE thread_goals SET status=?', (status,))
                self.assertTrue(self.transport._native_idle())
                with sqlite3.connect(self.home / 'goals_1.sqlite') as db:
                    self.assertEqual(db.execute('SELECT status FROM thread_goals').fetchone()[0], status)

    def test_active_or_unknown_goals_still_reserve_session(self):
        self.idle_native_with_goal('active')
        for status in ('active', 'unknown', 'budget_limited', 'usage_limited', '', None):
            with self.subTest(status=status):
                with sqlite3.connect(self.home / 'goals_1.sqlite') as db:
                    db.execute('UPDATE thread_goals SET status=?', (status,))
                self.assertFalse(self.transport._native_idle())

    def test_dormant_goal_does_not_override_running_turn_or_pending_input(self):
        self.idle_native_with_goal('blocked')
        for status in ('blocked', 'paused'):
            with self.subTest(status=status):
                with sqlite3.connect(self.home / 'goals_1.sqlite') as db:
                    db.execute('UPDATE thread_goals SET status=?', (status,))
                with sqlite3.connect(self.history) as db:
                    db.execute("UPDATE thread_turns SET status='inProgress'")
                self.assertFalse(self.transport._native_idle())
                with sqlite3.connect(self.history) as db:
                    db.execute("UPDATE thread_turns SET status='completed'")
                with sqlite3.connect(self.home / 'queue_1.sqlite') as db:
                    db.execute('INSERT INTO queued_items VALUES (?)', (SID,))
                self.assertFalse(self.transport._native_idle())
                with sqlite3.connect(self.home / 'queue_1.sqlite') as db:
                    db.execute('DELETE FROM queued_items')
                self.assertTrue(self.transport._native_idle())

    def test_goal_resumes_before_second_check_does_not_send(self):
        self.idle_native_with_goal('blocked')
        def target_then_resume():
            if not self.transport._native_idle():
                return None
            with sqlite3.connect(self.home / 'goals_1.sqlite') as db:
                db.execute("UPDATE thread_goals SET status='active'")
            return self.target
        self.transport._target.side_effect = target_then_resume
        result = self.submit()
        self.assertEqual(result['status'], 'queued')
        self.assertEqual(result['reason'], 'target_changed_before_send')
        self.assertIsNone(self.transport._lookup('event-1'))
        self.transport._orca.assert_not_called()

    def test_orca_working_state_stops_before_read_or_send(self):
        del self.transport._target
        self.transport._owner = Mock(return_value={'pid': 123, 'starttime': 100})
        self.transport._native_idle = Mock(return_value=True)
        self.transport._orca.side_effect = [
            {'terminals': [{'handle': 'fixture', 'tabId': 'tab', 'leafId': 'pane',
                'agentIdentity': 'codex', 'connected': True, 'writable': True, 'incarnationId': 'one'}]},
            {'worktrees': [{'worktreeId': 'repo::path', 'agents': [{'paneKey': 'tab:pane', 'state': 'working'}]}]}
        ]
        self.assertIsNone(self.transport._target())
        self.assertEqual(self.transport._orca.call_count, 2)


if __name__ == '__main__':
    unittest.main()
