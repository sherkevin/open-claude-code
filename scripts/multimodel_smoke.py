"""Opt-in real model switching/effort/continuity acceptance via product API."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
import uuid

from smoke_api import API, TERMINAL, require


CONFIGS = [('dano-v2.1', 'high', 'deepseek-v4.1-flash-1017'),
           ('dano-v2.1', 'light', 'deepseek-v4.1-flash-1017'),
           ('dano-v2.1', 'max', 'deepseek-v4.1-flash-1017'),
           ('deepseek-v4.1-flash', 'default', 'deepseek-v4.1-flash'),
           ('glm-5.3', 'default', 'glm-5.3')]


def identity_object(answer):
    value = json.loads(re.sub(r'^```(?:json)?\s*|\s*```$', '', answer.strip()))
    require(isinstance(value, dict), 'Identity response is not a JSON object')
    return value


def read_database(path):
    require(path.is_file(), 'Existing server database is required for read-only evidence')
    return sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)


def native_conversation(database, conversation_id):
    with closing(read_database(database)) as db:
        row = db.execute('SELECT sdk_session_id,model_snapshot FROM conversations WHERE id=?', (conversation_id,)).fetchone()
    require(row is not None, 'Synthetic conversation no longer exists')
    return row[0], json.loads(row[1])


def resumed_marker(source, labels):
    """Validate the four completed prefix selections; never rerun them."""
    runs = source.get('runs', [])
    require(len(runs) == 4, 'Resume requires the original four-pass report with only GLM unfinished')
    marker = None
    for run, (model, level, _) in zip(runs, CONFIGS[:4]):
        require(run.get('model_id') == model and run.get('reasoning_level') == level and run.get('status') == 'succeeded', 'Prior selections are not the expected four successful runs')
        value = identity_object(run.get('identity_answer', ''))
        require(str(value.get('current_public_model', '')).casefold() == labels[model].casefold(), 'A prior run failed strict public identity validation')
        current = value.get('marker')
        require(isinstance(current, str) and re.fullmatch(r'SWITCH-[a-f0-9]{20}', current), 'Prior synthetic marker is invalid')
        marker = marker or current
        require(current == marker, 'Prior synthetic markers differ')
    return marker


class StreamObservation:
    """Count raw stream event kinds without retaining reasoning or response text."""
    def __init__(self, database, run_id, started_at):
        self.database, self.run_id, self.started_at = database, run_id, started_at
        self.cursor = 0
        self.value = {'first_event_at': None, 'first_event_elapsed_ms': None,
                      'first_delta_at': None, 'first_delta_elapsed_ms': None,
                      'first_visible_text_at': None, 'first_visible_text_elapsed_ms': None,
                      'last_event_at': None, 'thinking_delta_events': 0, 'text_delta_events': 0,
                      'raw_events_seen': 0, 'observed_upstream_models': [], 'observed_session_ids': []}

    def mark(self, name, timestamp):
        if self.value[name + '_at'] is None:
            self.value[name + '_at'] = timestamp
            self.value[name + '_elapsed_ms'] = max(0, round((datetime.fromisoformat(timestamp) - datetime.fromisoformat(self.started_at)).total_seconds() * 1000))

    def update(self):
        with closing(read_database(self.database)) as db:
            rows = db.execute('SELECT id,payload,created_at FROM raw_events WHERE run_id=? AND id>? ORDER BY id', (self.run_id, self.cursor)).fetchall()
        for event_id, payload, timestamp in rows:
            self.cursor = event_id
            self.value['raw_events_seen'] += 1
            self.value['last_event_at'] = timestamp
            item = json.loads(payload)
            value = item.get('value', {})
            if item.get('type') == 'AssistantMessage':
                model = value.get('model')
                if model and model not in self.value['observed_upstream_models']:
                    self.value['observed_upstream_models'].append(model)
            if item.get('type') == 'ResultMessage':
                sid = value.get('session_id')
                if sid and sid not in self.value['observed_session_ids']:
                    self.value['observed_session_ids'].append(sid)
            if item.get('type') == 'StreamEvent':
                self.mark('first_event', timestamp)
                event = value.get('event', {})
                if event.get('type') == 'content_block_delta':
                    self.mark('first_delta', timestamp)
                    kind = event.get('delta', {}).get('type')
                    if kind == 'thinking_delta':
                        self.value['thinking_delta_events'] += 1
                    elif kind == 'text_delta':
                        self.mark('first_visible_text', timestamp)
                        self.value['text_delta_events'] += 1
        return self.value


def cleanup_resources(api, active, conversation, assistant, *, keep_resources=False, timeout=30):
    """Return cleanup errors instead of replacing the main test failure."""
    result = {'errors': [], 'retained_resources': False}
    terminal = not active
    if active:
        try:
            run = api.request('GET', '/runs/' + active)
            terminal = run['status'] in TERMINAL
            if not terminal:
                api.request('POST', '/runs/' + active + '/cancel', expected=(200, 202))
            deadline = time.monotonic() + timeout
            while not terminal:
                run = api.request('GET', '/runs/' + active)
                terminal = run['status'] in TERMINAL
                if terminal or time.monotonic() >= deadline:
                    break
                time.sleep(0.25)
            result['active_run_final_status'] = run['status']
            if not terminal:
                result['errors'].append({'operation': 'cancel_wait', 'error': 'Run did not reach a terminal state before cleanup deadline'})
        except Exception as exc:
            result['errors'].append({'operation': 'cancel_wait', 'error_type': type(exc).__name__, 'error': str(exc)})
    if keep_resources or not terminal:
        result['retained_resources'] = bool(conversation or assistant)
        return result
    archived = conversation is None
    if conversation:
        try:
            api.request('PATCH', '/conversations/' + conversation['id'], json={'archived': True})
            archived = True
            result['conversation_archived'] = True
        except Exception as exc:
            result['errors'].append({'operation': 'archive', 'error_type': type(exc).__name__, 'error': str(exc)})
    if assistant and archived:
        try:
            api.request('DELETE', '/assistants/' + assistant['id'], expected=(200, 204))
            result['assistant_deleted'] = True
        except Exception as exc:
            result['errors'].append({'operation': 'delete_assistant', 'error_type': type(exc).__name__, 'error': str(exc)})
    result['retained_resources'] = bool(result['errors'])
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--real-model', action='store_true')
    parser.add_argument('--password-file', type=Path)
    parser.add_argument('--report', type=Path, default=Path('.runtime/multimodel-report.json'))
    parser.add_argument('--data-dir', type=Path, default=Path('.runtime/data'))
    parser.add_argument('--timeout', type=float, default=300)
    parser.add_argument('--keep-resources', action='store_true')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--resume-from', type=Path)
    mode.add_argument('--fresh-model', choices=['dano-v2.1', 'deepseek-v4.1-flash', 'glm-5.3'])
    mode.add_argument('--glm-boundary-from', type=Path)
    args = parser.parse_args()
    if not args.real_model:
        print('Plan: four Dano/DeepSeek selections; --glm-boundary-from checks a protected historical switch then two fresh GLM turns; --fresh-model performs one isolated contrast. No calls made.')
        return
    require(0 < args.timeout <= 300, 'Timeout must be between zero and 300 seconds')
    require(args.password_file and args.password_file.is_file(), 'Provide private password file')
    require(not args.report.exists(), 'Use a fresh report path to retain earlier evidence')
    require(not args.resume_from or args.resume_from.resolve() != args.report.resolve(), 'Resume source and output report must differ')
    require(not args.glm_boundary_from or args.glm_boundary_from.resolve() != args.report.resolve(), 'Boundary source and output report must differ')
    database = args.data_dir / 'app.sqlite3'
    api = API('http://127.0.0.1:18080', 'http://127.0.0.1:18081')
    report = {'status': 'running', 'mode': 'resume' if args.resume_from else ('glm_boundary' if args.glm_boundary_from else ('fresh' if args.fresh_model else 'full')),
              'timeout_seconds': args.timeout, 'checks': [], 'runs': [],
              'boundary': 'Synthetic continuity and observed routing; not a quality benchmark or provider compute audit.'}
    assistant = conversation = None
    active = stream = None
    session_ids = []
    owns_resources = False

    def save():
        args.report.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

    try:
        save()
        api.login('admin', args.password_file.read_text().strip())
        options = api.request('GET', '/models')
        require({m['id'] for m in options} == {'dano-v2.1', 'deepseek-v4.1-flash', 'glm-5.3'}, 'Catalog IDs differ')
        labels = {m['id']: m['label'] for m in options}
        report['catalog'] = options
        if args.glm_boundary_from:
            source_bytes = args.glm_boundary_from.read_bytes()
            source = json.loads(source_bytes)
            original = api.request('GET', '/conversations/' + source['conversation_id'])
            require(not original['active_run_id'], 'Original synthetic conversation is still active')
            original_sid, original_selection = native_conversation(database, original['id'])
            require(original_sid, 'Boundary acceptance requires existing native history')
            glm = next(item for item in options if item['id'] == 'glm-5.3')
            require(glm.get('requires_new_conversation_on_switch') is True, 'GLM protected-switch capability is not enabled')
            target = 'deepseek-v4.1-flash' if original_selection['upstream_model'] == 'glm-5.3' else 'glm-5.3'
            original_messages = api.request('GET', f"/conversations/{original['id']}/messages")
            refused = api.request('PATCH', '/conversations/' + original['id'], expected=(409,), json={'model_id': target})
            require(refused.get('error', {}).get('code') == 'MODEL_NEW_CONVERSATION_REQUIRED', 'Protected historical model switch was not rejected')
            require(api.request('GET', '/conversations/' + original['id']) == original, 'Rejected switch changed the original conversation')
            require(api.request('GET', f"/conversations/{original['id']}/messages") == original_messages, 'Rejected switch changed original history')
            require(native_conversation(database, original['id']) == (original_sid, original_selection), 'Rejected switch changed the native session or route')
            report.update(boundary_source_sha256=hashlib.sha256(source_bytes).hexdigest(), original_conversation_id=original['id'], original_session_id=original_sid,
                          rejected_target=target, original_history_preserved=True)
            report['checks'].append('protected historical switch rejected without changing original conversation or history')
            save()
        if args.resume_from:
            source_bytes = args.resume_from.read_bytes()
            source = json.loads(source_bytes)
            marker = resumed_marker(source, labels)
            assistant = api.request('GET', '/assistants/' + source['assistant_id'])
            require(assistant.get('enabled') is True, 'Prior assistant is disabled; resume refuses to revive it')
            conversation = api.request('GET', '/conversations/' + source['conversation_id'])
            require(conversation['assistant_id'] == assistant['id'] and not conversation['archived'], 'Prior conversation is archived or belongs to another assistant')
            require(not conversation['active_run_id'], 'Prior run is still active; finish or cancel it before retrying')
            require(conversation['model_id'] == 'glm-5.3' and conversation['reasoning_level'] == 'default', 'Current GLM selection changed; resume will not modify model or effort')
            baseline_session, selection = native_conversation(database, conversation['id'])
            require(baseline_session and selection['upstream_model'] == 'glm-5.3', 'Prior native session or current private GLM route is unavailable')
            session_ids.append(baseline_session)
            report.update(resume_source_sha256=hashlib.sha256(source_bytes).hexdigest(), prior_passed_run_ids=[run['run_id'] for run in source['runs']], baseline_session_id=baseline_session)
            configs = [CONFIGS[-1]]
            owns_resources = True
        else:
            assistant = api.request('POST', '/assistants', expected=(200, 201), json={
                'name': '多模型合成验收 ' + uuid.uuid4().hex[:8], 'description': '仅合成记忆连续性',
                'instructions': '请完成用户合成验收，不调用工具，不写长期记忆。',
                'connections': [], 'access': {'all_members': False, 'user_ids': []}, 'enabled': True})
            owns_resources = True
            configs = [CONFIGS[-1], CONFIGS[-1]] if args.glm_boundary_from else ([next(item for item in CONFIGS if item[0] == args.fresh_model)] if args.fresh_model else CONFIGS[:4])
            model, level, _ = configs[0]
            conversation = api.request('POST', '/conversations', expected=(200, 201), json={
                'assistant_id': assistant['id'], 'title': '多模型连续性验收', 'model_id': model, 'reasoning_level': level})
            marker = 'SWITCH-' + secrets.token_hex(10)
        report.update(assistant_id=assistant['id'], conversation_id=conversation['id'])
        initial_messages = len(api.request('GET', f"/conversations/{conversation['id']}/messages"))
        save()
        for index, (model, level, upstream) in enumerate(configs):
            require(next(item for item in options if item['id'] == model)['available'], 'Selected model is unavailable')
            if index and configs[index] != configs[index - 1]:
                selected = api.request('PATCH', '/conversations/' + conversation['id'], json={'model_id': model, 'reasoning_level': level})
                require(selected['model_id'] == model and selected['reasoning_level'] == level, 'Selection not saved')
            prompt = (f'请记住会话校验码 {marker}。' if index == 0 and not args.resume_from else '请回忆之前约定的会话校验码。') + '只返回JSON对象，字段为 marker 和 current_public_model；后者填本轮当前公开模型名称。不要调用工具或读取记忆。'
            started_monotonic = time.monotonic()
            started = api.request('POST', f"/conversations/{conversation['id']}/messages", expected=(200, 202), json={'client_request_id': str(uuid.uuid4()), 'text': prompt, 'file_ids': []})
            active = started['run_id']
            run = api.request('GET', '/runs/' + active)
            stream = StreamObservation(database, active, run['created_at'])
            entry = {'run_id': active, 'model_id': model, 'reasoning_level': level, 'status': run['status'], 'acceptance_status': 'running', 'stream': stream.value}
            report['runs'].append(entry)
            save()
            if index == 0 and not args.resume_from:
                alternate = 'max' if model == 'dano-v2.1' else 'default'
                rejected = api.request('PATCH', '/conversations/' + conversation['id'], expected=(409,), json={'reasoning_level': alternate})
                require(rejected.get('error', {}).get('code') == 'CONVERSATION_BUSY', 'Active selection was not locked')
                report['checks'].append('active run blocks switching')
            deadline = started_monotonic + args.timeout
            next_save = time.monotonic()
            while True:
                run = api.request('GET', '/runs/' + active)
                entry.update(status=run['status'], elapsed_ms=round((time.monotonic() - started_monotonic) * 1000))
                stream.update()
                if time.monotonic() >= next_save:
                    save()
                    next_save = time.monotonic() + 5
                if run['status'] in TERMINAL:
                    break
                require(time.monotonic() < deadline, 'Run timed out; stream evidence distinguishes ongoing thinking from disconnection')
                time.sleep(0.4)
            require(run['status'] == 'succeeded', 'Model run failed')
            messages = api.request('GET', f"/conversations/{conversation['id']}/messages")
            answer = next(item['text'] for item in messages if item['id'] == run['assistant_message_id'])
            entry.update(identity_answer=answer, usage=run.get('usage'))
            identity = identity_object(answer)
            require(identity.get('marker') == marker, 'Native conversation marker differs')
            require(str(identity.get('current_public_model', '')).casefold() == labels[model].casefold(), 'Current public identity differs')
            require(run['model_id'] == model and run['reasoning_level'] == level, 'Public run selection differs')
            sid, selection = native_conversation(database, conversation['id'])
            with closing(read_database(database)) as db:
                private = json.loads(db.execute('SELECT execution_meta FROM runs WHERE id=?', (active,)).fetchone()[0])
            require(private['model'] == upstream and selection['upstream_model'] == upstream, 'Private routing snapshot differs')
            require(upstream in stream.value['observed_upstream_models'], 'Actual provider response model was not observed')
            require(sid in stream.value['observed_session_ids'], 'Native result session was not observed')
            session_ids.append(sid)
            if args.glm_boundary_from:
                require(sid != report['original_session_id'], 'Fresh GLM reused the original native session')
            entry.update(acceptance_status='passed', upstream_route_verified=True, native_session_id=sid)
            active = stream = None
            save()
            print(f'PASS {model}/{level}', flush=True)
        require(len(set(session_ids)) == 1 and session_ids[0], 'Native SDK session changed')
        require(len(api.request('GET', f"/conversations/{conversation['id']}/messages")) == initial_messages + 2 * len(configs), 'History size changed')
        report['checks'].extend(['same native SDK session retained', 'strict structured public identity and marker verified', 'actual provider response model observed', 'canonical history retained'])
        report['status'] = 'passed'
    except Exception as exc:
        report.update(status='failed', error_type=type(exc).__name__, error=str(exc))
        if report['runs'] and report['runs'][-1].get('acceptance_status') == 'running':
            report['runs'][-1]['acceptance_status'] = 'failed'
        save()  # Persist the primary failure before cancellation/archive can fail.
        raise
    finally:
        try:
            if owns_resources:
                report['cleanup'] = cleanup_resources(api, active, conversation, assistant, keep_resources=args.keep_resources)
            if stream is not None:
                try:
                    stream.update()
                except Exception as exc:
                    report.setdefault('cleanup', {}).setdefault('errors', []).append({'operation': 'final_stream_evidence', 'error_type': type(exc).__name__})
            save()
        except Exception as exc:
            print(f'Cleanup/report update failed: {type(exc).__name__}', flush=True)
        finally:
            try:
                api.close()
            except Exception as exc:
                print(f'Client cleanup failed: {type(exc).__name__}', flush=True)


if __name__ == '__main__':
    main()
