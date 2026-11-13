"""Submit to one existing Orca TUI and return only its exactly correlated reply.

There is deliberately no Codex resume/app-server fallback.  Native history is
read-only; the existing TUI remains the sole writer.  A request with an ambiguous
delivery is never automatically resent.  This transport does not send to Feishu.
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
import uuid


class TransportError(RuntimeError):
    """A fixed, non-sensitive reason code."""


@contextmanager
def _database(home: Path, prefix: str):
    candidates = []
    for p in home.glob(prefix + '*.sqlite'):
        m = re.fullmatch(re.escape(prefix) + r'(\d+)\.sqlite', p.name)
        if m:
            candidates.append((int(m.group(1)), p))
    if not candidates:
        raise TransportError('native_history_unavailable')
    path = max(candidates, key=lambda entry: entry[0])[1]
    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.5)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA query_only=ON')
    connection.execute('BEGIN')
    budget = [0]

    def bounded_query():
        budget[0] += 1
        return int(budget[0] > 3000)

    connection.set_progress_handler(bounded_query, 1000)
    try:
        yield connection
    finally:
        connection.close()


def _text(item: dict) -> str | None:
    """Only a plain textual user message can match a transport payload."""
    content = item.get('content')
    if not isinstance(content, list) or not content:
        return None
    if any(not isinstance(part, dict) or part.get('type') != 'text' or
           not isinstance(part.get('text'), str) for part in content):
        return None
    return ''.join(part['text'] for part in content)


class OrcaTransport:
    """Config: session_id, codex_home, cwd, worktree_id, pane_key,
    codex_binary, orca_command (list), state_dir; optional orca_data.

    submit()/poll() return request_id and status.  queued means no input was
    sent; submitted/uncertain must only be polled.  Only completed has final.
    The bridge must separately parse its explicit group-reply JSON envelope.
    """

    def __init__(self, config: dict):
        self.config = dict(config)
        self.session_id = str(uuid.UUID(config['session_id']))
        self.home = Path(config['codex_home']).resolve()
        self.cwd = Path(config['cwd']).resolve()
        self.command = config['orca_command']
        if not isinstance(self.command, list) or not self.command or not all(
                isinstance(x, str) and x for x in self.command):
            raise ValueError('orca_command must be a nonempty argv list')
        self.state_dir = Path(config['state_dir']).resolve()
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.state_dir, 0o700)
        self.state_path = self.state_dir / 'orca_transport.sqlite'
        with self._state() as db:
            db.execute('CREATE TABLE IF NOT EXISTS requests ('
                       'request_id TEXT PRIMARY KEY, payload TEXT NOT NULL, digest TEXT NOT NULL, '
                       'baseline INTEGER NOT NULL, status TEXT NOT NULL, target_json TEXT NOT NULL, '
                       'receipt_json TEXT, turn_id TEXT, reason TEXT, created_at REAL NOT NULL)')
        os.chmod(self.state_path, 0o600)

    @contextmanager
    def _state(self):
        db = sqlite3.connect(self.state_path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _orca(self, *args):
        environment = dict(os.environ)
        if self.config.get('orca_data'):
            environment['ORCA_USER_DATA_PATH'] = self.config['orca_data']
        result = subprocess.run([*self.command, *args, '--json'], env=environment,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=25)
        try:
            body = json.loads(result.stdout)
        except (ValueError, UnicodeError):
            raise TransportError('orca_response_unreadable') from None
        if result.returncode or not isinstance(body, dict) or body.get('ok') is False:
            raise TransportError('orca_request_not_verified')
        value = body.get('result', body)
        if not isinstance(value, dict):
            raise TransportError('orca_response_unreadable')
        return value

    def _owner(self) -> dict:
        """Require an actual kernel writer lock, exact executable and pane.

        A Codex process may own its child agents' locks too.  Those are allowed
        only when the state store identifies them as descendants of this root.
        """
        lock_path = self.home / 'thread-writer-locks' / (self.session_id + '.lock')
        binary = str(Path(self.config['codex_binary']).resolve())
        owners = []
        for proc in Path('/proc').iterdir():
            if not proc.name.isdecimal():
                continue
            try:
                if os.readlink(proc / 'exe') != binary:
                    continue
                env = dict(part.split(b'=', 1) for part in (proc / 'environ').read_bytes().split(b'\0')
                           if b'=' in part)
                if env.get(b'ORCA_PANE_KEY', b'').decode() != self.config['pane_key']:
                    continue
                if Path(os.fsdecode(env.get(b'CODEX_HOME', b''))).resolve() != self.home:
                    continue
                if Path(os.readlink(proc / 'cwd')).resolve() != self.cwd:
                    continue
                owned = set()
                for fd in (proc / 'fd').iterdir():
                    try:
                        target = Path(os.readlink(fd))
                        if target.parent != lock_path.parent:
                            continue
                        info = (proc / 'fdinfo' / fd.name).read_text()
                        if re.search(r'\bWRITE\s+' + re.escape(proc.name) + r'\s', info):
                            owned.add(str(uuid.UUID(target.stem)))
                    except (OSError, ValueError):
                        continue
                if self.session_id not in owned:
                    continue
                stat = (proc / 'stat').read_text()
                starttime = int(stat[stat.rindex(')') + 2:].split()[19])
                owners.append({'pid': int(proc.name), 'starttime': starttime, 'owned': sorted(owned)})
            except (OSError, ValueError, UnicodeError):
                continue
        if len(owners) != 1:
            raise TransportError('session_writer_not_unique')
        owner = owners[0]
        with _database(self.home, 'state_') as db:
            rows = {sid: db.execute('SELECT source,cwd,archived FROM threads WHERE id=?', (sid,)).fetchone()
                    for sid in owner['owned']}
        root = rows[self.session_id]
        if not root or root['source'] != 'cli' or root['archived'] or Path(root['cwd']).resolve() != self.cwd:
            raise TransportError('session_identity_mismatch')
        parents = {}
        for sid, row in rows.items():
            if sid == self.session_id:
                continue
            try:
                parents[sid] = json.loads(row['source'])['subagent']['thread_spawn']['parent_thread_id']
            except (TypeError, KeyError, ValueError):
                raise TransportError('multiple_root_sessions_in_pane') from None
        for sid in parents:
            seen = set()
            while sid != self.session_id:
                if sid in seen or sid not in parents:
                    raise TransportError('multiple_root_sessions_in_pane')
                seen.add(sid)
                sid = parents[sid]
        return {'pid': owner['pid'], 'starttime': owner['starttime']}

    def _native_idle(self):
        with _database(self.home, 'thread_history_') as db:
            latest = db.execute('SELECT status FROM thread_turns WHERE thread_id=? '
                                'ORDER BY rollout_ordinal DESC LIMIT 1', (self.session_id,)).fetchone()
            if not latest or latest['status'] not in ('completed', 'interrupted', 'failed'):
                return False
        with _database(self.home, 'queue_') as db:
            if db.execute('SELECT 1 FROM queued_items WHERE thread_id=? LIMIT 1', (self.session_id,)).fetchone():
                return False
        with _database(self.home, 'goals_') as db:
            goal = db.execute('SELECT status FROM thread_goals WHERE thread_id=?', (self.session_id,)).fetchone()
            # Dormant goals do not own an idle turn. Keep active and unknown
            # states closed; the turn, input queue and composer guards still apply.
            if goal and goal['status'] not in ('completed', 'blocked', 'paused'):
                return False
        return True

    @staticmethod
    def _empty_composer(screen):
        terminal = screen.get('terminal', {})
        lines = terminal.get('tail')
        if not isinstance(lines, list) or not all(isinstance(line, str) for line in lines):
            return False
        prompts = [(i, re.match(r'^\s*[›»]\s*(.*)$', line)) for i, line in enumerate(lines)]
        prompts = [(i, match.group(1).strip()) for i, match in prompts if match]
        if not prompts:
            return False
        index, draft = prompts[-1]
        # This is the known empty Codex composer, not any previous transcript
        # prompt.  Requiring the footer also rejects open menus and scrollback.
        footer = '\n'.join(lines[index + 1:])
        return draft == 'Ask Codex to do anything' and '? for shortcuts' in footer

    def _target(self):
        listing = self._orca('terminal', 'list', '--worktree', 'id:' + self.config['worktree_id'])
        matches = [t for t in listing.get('terminals', []) if
                   str(t.get('tabId')) + ':' + str(t.get('leafId')) == self.config['pane_key']]
        if len(matches) != 1:
            raise TransportError('terminal_not_unique')
        terminal = matches[0]
        if terminal.get('agentIdentity') != 'codex' or not terminal.get('connected') or not terminal.get('writable'):
            raise TransportError('terminal_not_ready')
        if not terminal.get('handle') or not terminal.get('incarnationId'):
            raise TransportError('terminal_identity_missing')
        owner = self._owner()
        agents = [agent for workspace in self._orca('worktree', 'ps').get('worktrees', [])
                  if workspace.get('worktreeId') == self.config['worktree_id']
                  for agent in workspace.get('agents', []) if agent.get('paneKey') == self.config['pane_key']]
        if len(agents) != 1 or agents[0].get('state') not in ('done', 'idle') or not self._native_idle():
            return None
        screen = self._orca('terminal', 'read', '--terminal', terminal['handle'], '--screen')
        if not self._empty_composer(screen):
            return None
        return {**owner, 'handle': terminal['handle'], 'incarnation': terminal['incarnationId'],
                'pane_key': self.config['pane_key'], 'session_id': self.session_id}

    def probe(self):
        """Read-only readiness inspection; never includes conversation text."""
        try:
            target = self._target()
            return {'status': 'ready' if target else 'busy', 'session_id': self.session_id,
                    **({'target': target} if target else {})}
        except (TransportError, OSError, sqlite3.Error, subprocess.SubprocessError):
            return {'status': 'unavailable', 'session_id': self.session_id}

    def _payload(self, request_id, prompt):
        identity = hashlib.sha256((self.session_id + '\0' + request_id).encode()).hexdigest()
        return '[feishu-project-request:' + identity + ']\n' + prompt

    def _lookup(self, request_id):
        with self._state() as db:
            return db.execute('SELECT * FROM requests WHERE request_id=?', (request_id,)).fetchone()

    def submit(self, request_id: str, prompt: str) -> dict:
        result = {'request_id': request_id}
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', request_id):
            return {**result, 'status': 'failed', 'reason': 'invalid_request_id'}
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 30000 or any(
                (ord(ch) < 32 and ch not in '\n\t') or ord(ch) == 127 for ch in prompt):
            return {**result, 'status': 'failed', 'reason': 'invalid_prompt'}
        payload = self._payload(request_id, prompt)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        old = self._lookup(request_id)
        if old:
            if old['digest'] != digest:
                return {**result, 'status': 'failed', 'reason': 'request_payload_conflict'}
            return self.poll(request_id)
        try:
            target = self._target()
            if target is None:
                return {**result, 'status': 'queued', 'reason': 'target_busy'}
            with _database(self.home, 'thread_history_') as history:
                baseline = history.execute('SELECT COALESCE(MAX(rollout_ordinal),-1) FROM thread_items '
                                           'WHERE thread_id=?', (self.session_id,)).fetchone()[0]
            # A second snapshot narrows PID/PTY/replacement and local-user races.
            # Orca does not offer an atomic expected PID + empty composer CAS.
            if self._target() != target:
                return {**result, 'status': 'queued', 'reason': 'target_changed_before_send'}
        except (TransportError, OSError, sqlite3.Error, subprocess.SubprocessError):
            return {**result, 'status': 'queued', 'reason': 'target_unavailable'}
        with self._state() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT digest FROM requests WHERE request_id=?', (request_id,)).fetchone()
            if existing:
                db.rollback()
                if existing['digest'] != digest:
                    return {**result, 'status': 'failed', 'reason': 'request_payload_conflict'}
                return self.poll(request_id)
            db.execute('INSERT INTO requests (request_id,payload,digest,baseline,status,target_json,created_at) '
                       'VALUES (?,?,?,?,?,?,?)', (request_id, payload, digest, baseline, 'sending',
                                                json.dumps(target), time.time()))
        try:
            receipt = self._orca('terminal', 'send', '--terminal', target['handle'],
                                 '--text', payload, '--enter', '--wait-submit', '1')
            send = receipt.get('send', receipt)
            proof = send.get('prompt', {})
            accepted = send.get('accepted') is True
            supported = proof.get('provider') == 'codex' and proof.get('requestId') and \
                'input_accepted' in proof.get('stages', []) and proof.get('observation') == 'supported'
            status = 'submitted' if accepted and supported else 'uncertain'
            # Persist only receipt metadata, never terminal output or prompt body.
            safe_receipt = {key: proof.get(key) for key in (
                'requestId', 'stages', 'provider', 'observation', 'processIncarnation', 'generation')}
            with self._state() as db:
                db.execute('UPDATE requests SET status=?,receipt_json=? WHERE request_id=?',
                           (status, json.dumps(safe_receipt), request_id))
            return {**result, 'status': status, 'orca_request_id': proof.get('requestId')}
        except (TransportError, OSError, sqlite3.Error, subprocess.SubprocessError, ValueError, TypeError):
            with self._state() as db:
                db.execute('UPDATE requests SET status=?,reason=? WHERE request_id=?',
                           ('uncertain', 'delivery_unknown', request_id))
            return {**result, 'status': 'uncertain', 'reason': 'delivery_unknown'}

    def poll(self, request_id: str) -> dict:
        row = self._lookup(request_id)
        result = {'request_id': request_id}
        if not row:
            # A bridge crash between its dispatching record and submit is
            # ambiguous; it must not infer permission to send a fresh request.
            return {**result, 'status': 'uncertain', 'reason': 'request_not_recorded'}
        try:
            return self._poll_history(row)
        except (TransportError, sqlite3.Error, OSError, ValueError, TypeError, KeyError):
            return {**result, 'status': 'uncertain', 'reason': 'history_not_verified'}

    def _poll_history(self, row):
        result = {'request_id': row['request_id']}
        if row['reason'] in ('mixed_user_input', 'ambiguous_request_turn'):
            return {**result, 'status': 'uncertain', 'reason': row['reason']}
        with _database(self.home, 'thread_history_') as db:
            messages = db.execute('SELECT turn_id,item_id,item_json FROM thread_items '
                                  'WHERE thread_id=? AND item_type=? AND rollout_ordinal>? '
                                  'ORDER BY rollout_ordinal LIMIT 1001',
                                  (self.session_id, 'userMessage', row['baseline'])).fetchall()
            if len(messages) > 1000:
                raise TransportError('history_scan_limit')
            matches = [message for message in messages if _text(json.loads(message['item_json'])) == row['payload']]
            if not matches:
                return {**result, 'status': 'submitted' if row['status'] == 'submitted' else 'uncertain',
                        'reason': 'awaiting_exact_user_item'}
            if len(matches) != 1:
                return self._suppress(row, 'ambiguous_request_turn')
            message = matches[0]
            turn_id = message['turn_id']
            turn = db.execute('SELECT * FROM thread_turns WHERE thread_id=? AND turn_id=?',
                              (self.session_id, turn_id)).fetchone()
            if not turn:
                raise TransportError('turn_not_found')
            users = db.execute('SELECT item_id,item_json FROM thread_items WHERE thread_id=? '
                               'AND turn_id=? AND item_type=?',
                               (self.session_id, turn_id, 'userMessage')).fetchall()
            if len(users) != 1 or turn['first_user_item_id'] != message['item_id'] or \
                    _text(json.loads(users[0]['item_json'])) != row['payload']:
                return self._suppress(row, 'mixed_user_input')
            with self._state() as state:
                state.execute('UPDATE requests SET turn_id=? WHERE request_id=?', (turn_id, row['request_id']))
            result['turn_id'] = turn_id
            if turn['status'] in ('failed', 'interrupted') or turn['error_json'] not in (None, 'null'):
                return {**result, 'status': 'failed', 'reason': 'native_turn_failed'}
            if turn['status'] != 'completed':
                return {**result, 'status': 'submitted'}
            item_id = turn['final_agent_item_id']
            if not item_id or turn['completed_at'] is None:
                raise TransportError('final_item_not_available')
            final = db.execute('SELECT item_type,item_json FROM thread_items WHERE thread_id=? '
                               'AND turn_id=? AND item_id=?', (self.session_id, turn_id, item_id)).fetchone()
            if not final or final['item_type'] != 'agentMessage':
                raise TransportError('final_item_not_verified')
            item = json.loads(final['item_json'])
            if item.get('type') != 'agentMessage' or item.get('id') != item_id or \
                    item.get('phase') != 'final_answer' or not isinstance(item.get('text'), str) or not item['text'].strip():
                raise TransportError('final_item_not_verified')
            return {**result, 'status': 'completed', 'final': item['text'], 'final_item_id': item_id}

    def _suppress(self, row, reason):
        with self._state() as db:
            db.execute('UPDATE requests SET status=?,reason=? WHERE request_id=?',
                       ('uncertain', reason, row['request_id']))
        return {'request_id': row['request_id'], 'status': 'uncertain', 'reason': reason}
