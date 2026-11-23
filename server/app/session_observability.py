"""Owner-scoped conversation receipts; never read native logs or reasoning.

These are projections of host-owned usage, messages and public tool events.
Missing provider counters and hypothetical compression revenue stay unknown.
"""
from __future__ import annotations

import base64
import json
import math
from collections import defaultdict

from .db import now
from .security import AppError
from .usage_accounting import _entry, _object, recover_terminal_usage

MAX_SESSIONS = 1000
MAX_RUNS = 20000
MAX_COMPACTION_EVENTS = 20000
MAX_TEXT = 4000
TERMINAL = frozenset({'succeeded', 'failed', 'cancelled', 'interrupted'})
TOKEN_NAMES = {'input_tokens': 'input_tokens', 'output_tokens': 'output_tokens',
               'cache_read_tokens': 'cache_read_input_tokens', 'cache_write_tokens': 'cache_creation_input_tokens'}
LEDGER_TOKEN_NAMES = {**TOKEN_NAMES, 'cache_write_tokens': 'cache_write_input_tokens'}
INPUT_BUCKETS = ('input_tokens', 'cache_read_tokens', 'cache_write_tokens')
USAGE_FIELDS = ('request_count', *TOKEN_NAMES, 'total_input_tokens', 'cost_usd')


def _number(value, *, integer=True):
    valid = type(value) is int if integer else type(value) in (int, float)
    return value if valid and 0 <= value <= 10**15 and math.isfinite(value) else None


def _reading(value, known, observed_count, total_count, *, estimated=False):
    result = {'value': value, 'observed_count': observed_count, 'total_count': total_count}
    if value is not None:
        result['precision'] = 'estimated' if estimated else 'observed'
    elif known is not None:
        result.update(precision='partial', known_value=known)
        if estimated:
            result['known_estimated'] = True
    else:
        result['precision'] = 'unavailable'
    return result


def _runs(service, user_id, conversation_id=None):
    where = 'c.user_id=? AND r.user_id=?'
    values = [user_id, user_id]
    if conversation_id is not None:
        where += ' AND c.id=?'
        values.append(conversation_id)
    sql = ' FROM runs r JOIN conversations c ON c.id=r.conversation_id WHERE ' + where
    total = service.db.one('SELECT COUNT(*) n' + sql, values)['n']
    rows = service.db.all('SELECT r.*' + sql + ' ORDER BY r.created_at DESC,r.id DESC LIMIT ?', (*values, MAX_RUNS))
    recover_terminal_usage(service.db, rows)
    return rows, total


def _prompt(reported, buckets):
    parts = [value for value in buckets if value is not None]
    subtotal = sum(parts)
    complete = len(parts) == len(buckets)
    if reported is not None:
        if subtotal > reported or (complete and subtotal != reported):
            return None, None
        return reported, reported
    return (subtotal if complete else None), (subtotal if parts else None)


def _run_usage(row):
    entry = _entry(row)
    usage = _object(row.get('usage'))
    ledger = _object(usage.get('token_accounting'))
    raw_stages = ledger.get('stages')
    http_capture = ledger.get('receipt_schema') == 'harness-http-usage/v1'
    stages = [stage for stage in raw_stages if isinstance(stage, dict) and stage.get('provider_call') is True
              and (not http_capture or stage.get('source') == 'harness_provider')] if isinstance(raw_stages, list) else []
    stage_tokens = []
    for stage in stages:
        provider = _object(stage.get('provider_usage'))
        tokens = {key: _number(provider.get(field)) for key, field in LEDGER_TOKEN_NAMES.items()}
        total, known_total = _prompt(_number(stage.get('reported_prompt_tokens')), [tokens[key] for key in INPUT_BUCKETS])
        stage_tokens.append({**tokens, 'prompt': total, 'known_prompt': known_total})
    product = (_object(row.get('execution_meta')).get('execution_kind') == 'product_metadata'
               or usage.get('source') == 'product_metadata')
    calls = entry['call_count']
    attempts = sum(stage.get('source') == 'harness_provider' for stage in stages)
    full = {field: entry['amounts'][key] for field, key in TOKEN_NAMES.items()}
    if http_capture and calls is None:
        full = dict.fromkeys(TOKEN_NAMES)
    total_input = entry['total_input_tokens']
    if http_capture and (calls is None or any(tokens['prompt'] is None for tokens in stage_tokens)):
        total_input = None
    full.update(request_count=calls, cost_usd=entry['amounts']['estimated_cost_usd'],
                total_input_tokens=total_input)
    known = dict(full)
    if calls is None and attempts:
        known['request_count'] = attempts
    for key, field in TOKEN_NAMES.items():
        if known[key] is None:
            observations = [tokens[key] for tokens in stage_tokens if tokens[key] is not None]
            if observations:
                known[key] = sum(observations)
            elif raw_stages is None and entry['known'][field] > 0:
                known[key] = entry['known'][field]

    denominator, numerator = full['total_input_tokens'], full['cache_read_tokens']
    paired = denominator is not None and numerator is not None and numerator <= denominator
    pairs = [(denominator, numerator)] if paired else []
    if not paired:
        pairs = [(tokens['prompt'], tokens['cache_read_tokens']) for tokens in stage_tokens
                 if tokens['prompt'] is not None and tokens['cache_read_tokens'] is not None]
    if known['total_input_tokens'] is None:
        prompts = [tokens['known_prompt'] for tokens in stage_tokens if tokens['known_prompt'] is not None]
        if prompts:
            known['total_input_tokens'] = sum(prompts)
        elif not stages:
            reported = _number(usage.get('total_input_tokens' if http_capture else 'reported_prompt_tokens'))
            _, known['total_input_tokens'] = _prompt(reported, [known[key] for key in INPUT_BUCKETS])
    return {'full': full, 'known': known, 'paired': paired, 'pairs': pairs, 'reported': entry['reported'],
            'cost_estimated': full['cost_usd'] is not None and not product,
            'proved_no_compaction': product or (http_capture and calls is not None and row['status'] == 'succeeded'
                                                and all(stage.get('stage') == 'answer' for stage in stages))}


def _usage(samples, total):
    result = {}
    for field in USAGE_FIELDS:
        observations = [sample['known'][field] for sample in samples if sample['known'][field] is not None]
        complete = all(sample['full'][field] is not None for sample in samples) and len(samples) == total
        if field == 'cost_usd' and not complete:
            # Local-only executions cost zero, but supply no evidence of model charges.
            observations = [sample['known'][field] for sample in samples if sample['cost_estimated']]
        known = sum(observations) if observations else None
        value = sum(observations) if complete and (field != 'cost_usd' or observations) else None
        result[field] = _reading(value, known, len(observations), total,
                                 estimated=field == 'cost_usd' and any(sample['cost_estimated'] for sample in samples))
    denominator = sum(total for sample in samples for total, _ in sample['pairs'])
    numerator = sum(read for sample in samples for _, read in sample['pairs'])
    rate = numerator / denominator if denominator > 0 else None
    complete = len(samples) == total and all(sample['paired'] for sample in samples)
    result['cache_hit_rate'] = _reading(rate if complete else None, rate,
                                       sum(bool(sample['pairs']) for sample in samples), total)
    return result


def _values(readings, fields):
    result = {key: readings[key]['value'] for key in fields}
    known = {key: readings[key]['known_value'] for key in fields if 'known_value' in readings[key]}
    if known:
        result['known_values'] = known
        estimates = [key for key in known if readings[key].get('known_estimated')]
        if estimates:
            result['known_estimated_fields'] = estimates
    return result


def _context_value(snapshot):
    snapshot = _object(snapshot)
    if snapshot.get('stale') or snapshot.get('error'):
        return None, None
    value = _number(snapshot.get('total_tokens'), integer=False)
    if snapshot.get('source') == 'native_runtime' and snapshot.get('status') == 'available':
        return value, 'observed'
    if (snapshot.get('source') == 'harness_token_meter' and snapshot.get('precision') == 'estimated'
            and snapshot.get('status') in {'available', 'partial'}):
        return value, 'estimated'
    return None, None


def _compression_difference(record):
    if record.get('measurement_schema') == 'harness-estimated/v1':
        # The Harness publishes these together after a committed automatic
        # replacement, using its local token meter on the same session.
        first, last = _number(record.get('pre_tokens')), _number(record.get('post_tokens'))
        return (first - last, 'estimated') if first is not None and last is not None else (None, 'unavailable')
    before, after = _object(record.get('before')), _object(record.get('after'))
    first, first_precision = _context_value(before)
    last, last_precision = _context_value(after)
    if (first is None or last is None or before.get('source') != after.get('source')
            or before.get('model') != after.get('model')
            or before.get('_binding') != after.get('_binding')):
        return None, 'unavailable'
    return first - last, 'estimated' if 'estimated' in {first_precision, last_precision} else 'observed'


def _compression_measurement(record):
    difference, precision = _compression_difference(record)
    cost = None
    if record.get('measurement_schema') == 'compaction-measurement/v1':
        receipt = _object(record.get('cost_usage'))
        ledger = _object(receipt.get('token_accounting'))
        stages = ledger.get('stages')
        # Only the operation's own committed summary receipt is a cost. Never
        # substitute the whole run, an answer, or historical guessed usage.
        if (isinstance(stages, list) and stages
                and not (ledger.get('receipt_schema') == 'harness-http-usage/v1' and ledger.get('capture_complete') is not True)):
            costs = []
            for value in stages:
                stage = _object(value)
                if not (stage.get('stage') == 'compression' and stage.get('provider_call') is True
                        and stage.get('source') == 'harness_provider'):
                    break
                usage = _object(stage.get('provider_usage'))
                buckets = [_number(usage.get(key)) for key in ('input_tokens', 'cache_read_input_tokens', 'cache_write_input_tokens')]
                total, _ = _prompt(_number(stage.get('reported_prompt_tokens')), buckets)
                output = _number(usage.get('output_tokens'))
                if total is None or output is None:
                    break
                costs.append(total + output)
            if len(costs) == len(stages):
                cost = sum(costs)
    return {'difference': difference, 'precision': precision, 'cost': cost}


def _auto_compaction(record):
    return (record.get('status') == 'completed' and record.get('measurement_schema') == 'harness-estimated/v1'
            and record.get('trigger') in {'pressure', 'context-overflow'})


def _compression_records(stored, events):
    boundaries, automatic, results = [], [], []
    for event in events:
        record = _object(event['data'])
        if event['type'] == 'context.compacted':
            boundaries.append(record)
        elif _auto_compaction(record):
            automatic.append(record)
        else:
            results.append(record)

    if _auto_compaction(stored):
        # Automatic events carry each operation. The stored copy mirrors the
        # latest result and also covers a crash between the two writes.
        if stored not in automatic:
            automatic.append(stored)
    elif stored:
        results.append(stored)

    final = next((record for record in reversed(results)
                  if record.get('status') == 'completed' and (record.get('boundary_observed') is True or boundaries)), None)
    if final is None:
        final = next((record for record in reversed(results) if record.get('boundary_observed') is True), None)
    if final is not None:
        # The validated manual result describes the last native boundary.
        boundaries = boundaries[:-1] + [final]
    no_op = any(record.get('status') == 'not_needed' and record.get('boundary_observed') is not True for record in results)
    return boundaries + automatic, no_op


def _compressions(service, rows, usage_by_run):
    events = defaultdict(list)
    truncated = set()
    ids = [row['id'] for row in rows]
    remaining = MAX_COMPACTION_EVENTS
    for offset in range(0, len(ids), 400):
        batch = ids[offset:offset + 400]
        found = service.db.all('SELECT run_id,type,data FROM events WHERE run_id IN (' + ','.join('?' for _ in batch) +
            ") AND type IN ('context.compacted','context.compaction_result') ORDER BY run_id,id LIMIT ?", (*batch, remaining + 1))
        if len(found) > remaining:
            truncated.update(run_id for run_id in batch if run_id >= found[remaining]['run_id'])
        for event in found[:remaining]:
            events[event['run_id']].append(event)
        remaining -= min(len(found), remaining)
    result = defaultdict(list)
    for row in rows:
        records, no_op = _compression_records(_object(row.get('compaction')), events[row['id']])
        complete = row['id'] not in truncated and (
            no_op or usage_by_run[row['id']]['proved_no_compaction']
            or bool(records) and (row['status'] == 'succeeded' or row.get('kind') == 'compact'))
        result[row['conversation_id']].append(([_compression_measurement(item) for item in records], complete))
    return result, bool(truncated)


def _compression_summary(samples, total):
    records = [item for measurements, _ in samples for item in measurements]
    complete = len(samples) == total and all(covered for _, covered in samples)
    tracked = sum(bool(measurements) or covered for measurements, covered in samples)
    count = _reading(len(records) if complete else None, len(records) if tracked else None, tracked, total)
    differences = [item for item in records if item['difference'] is not None]
    costs = [item['cost'] for item in records if item['cost'] is not None]
    nets = [item['difference'] - item['cost'] for item in differences if item['cost'] is not None]

    def subtotal(values, *, estimated=False):
        known = sum(values) if values else None
        return _reading(known if complete and len(values) == len(records) else None, known, len(values),
                        len(records), estimated=estimated)

    difference = subtotal([item['difference'] for item in differences],
                          estimated=any(item['precision'] == 'estimated' for item in differences))
    cost = subtotal(costs)
    net = subtotal(nets, estimated=True)
    # This is a what-if for ONE reuse per operation, never actual billed savings.
    # A single-operation token break-even is meaningful; pooling successive
    # overlapping compactions into a reuse count would invent a future workload.
    value = difference['value']
    break_even = (math.ceil(cost['value'] / value)
                  if len(records) == 1 and cost['value'] is not None and value is not None and value > 0 else None)
    return {'compression_count': count, 'compression_saved_tokens': difference,
            'compression_cost_tokens': cost, 'net_saved_tokens': net,
            'compression_return_usd': _reading(None, None, 0, len(records))}, break_even


def overview(service, user):
    """Include archived conversations, but never another user's conversations."""
    with service.db.lock:
        count = service.db.one('SELECT COUNT(*) n FROM conversations WHERE user_id=?', (user['id'],))['n']
        conversations = service.db.all('SELECT id,title,updated_at,archived,active_run_id FROM conversations WHERE user_id=? '
            'ORDER BY updated_at DESC,id DESC LIMIT ?', (user['id'], MAX_SESSIONS))
        rows, total = _runs(service, user['id'])
        usage_by_run = {row['id']: _run_usage(row) for row in rows}
        all_usage = _usage(list(usage_by_run.values()), total)
        by_session = defaultdict(list)
        for row in rows:
            by_session[row['conversation_id']].append(usage_by_run[row['id']])
        run_counts = {row['conversation_id']: row['n'] for row in service.db.all(
            'SELECT r.conversation_id,COUNT(*) n FROM runs r JOIN conversations c ON c.id=r.conversation_id '
            'WHERE c.user_id=? AND r.user_id=? GROUP BY r.conversation_id', (user['id'], user['id']))}
        compression, events_truncated = _compressions(service, rows, usage_by_run)
        incomplete = total != len(rows)
        saved, break_even = _compression_summary([item for values in compression.values() for item in values], total)
        sessions = []
        for conversation in conversations:
            run_count = run_counts.get(conversation['id'], 0)
            usage = _usage(by_session[conversation['id']], run_count)
            session_saved, session_break_even = _compression_summary(compression[conversation['id']], run_count)
            tokens = [usage[key] for key in TOKEN_NAMES]
            token_precision = ('observed' if all(item['value'] is not None for item in tokens)
                               else 'partial' if any(item['observed_count'] for item in tokens) else 'unavailable')
            sessions.append({**{key: conversation[key] for key in ('id', 'title', 'updated_at')},
                'status': 'running' if conversation['active_run_id'] else 'archived' if conversation['archived'] else 'idle',
                'run_count': run_count, **_values({**usage, **session_saved}, (*USAGE_FIELDS, *session_saved)),
                'token_precision': token_precision, 'request_precision': usage['request_count']['precision'],
                'cost_precision': usage['cost_usd']['precision'],
                'compression_count_precision': session_saved['compression_count']['precision'],
                'compression_precision': session_saved['compression_saved_tokens']['precision'],
                'net_precision': session_saved['net_saved_tokens']['precision'],
                'compression_break_even_reuses': session_break_even})
        metrics = []
        def add(identity, title, reading, unit, note, scope=None):
            if scope and reading['total_count']:
                note = f"覆盖 {reading['observed_count']}/{reading['total_count']} {scope}。" + note
            metrics.append({'id': identity, 'title': title, **reading, 'unit': unit, 'note': note})

        add('session_count', '会话总数', _reading(count, None, count, count), '个', '包含当前账号的已归档会话。')
        add('request_count', '模型请求总数', all_usage['request_count'], '次',
            '统计已记录的请求尝试。失败或取消的尝试不证明供应商收到或收费。SDK 汇总回执无法核实尝试次数。', '条执行记录')
        add('compression_count', '压缩次数', saved['compression_count'], '次',
            '统计已确认提交的自动和手动压缩。已确认未发生时为零，历史回执缺失时保留未知。', '条执行记录')
        add('compression_saved_tokens', '压缩上下文减少', saved['compression_saved_tokens'], 'Token',
            '累计同一次压缩的前后上下文差额。Harness 本地计数属于估算。', '次已确认压缩')
        cost_value = saved['compression_cost_tokens']['value']
        cost_note = f"已记录摘要成本 {cost_value} Token。" if cost_value is not None else '摘要完整成本未记录。'
        add('net_saved_tokens', '净节省（单次回用估算）', saved['net_saved_tokens'], 'Token',
            '单次回用估算假设每次压缩后的上下文完整回用 1 次。每对样本用前后差额减去该次摘要的全部输入与输出 Token。'
            '净值可能为负，账单节省需另行核实。' + cost_note, '次已确认压缩')
        return_note = (f"当前单次压缩按相同上下文差额回用约 {break_even} 次可抵摘要 Token 成本。"
                       if break_even is not None else '')
        add('compression_return_usd', '压缩回本', saved['compression_return_usd'], 'USD',
            '美元收益需要未压缩对照请求的实际输入、缓存变化与适用价格。' + return_note)
        add('cache_hit_rate', '缓存命中率', all_usage['cache_hit_rate'], 'ratio',
            '用同一组完整样本的缓存读取量除以含缓存的输入总量。仅使用同条执行或同次请求中成对记录的计数。'
            '分母为零时无比率。', '条执行记录')
        add('input_tokens', '输入 Token', all_usage['total_input_tokens'], 'Token',
            '总输入含缓存。每个样本优先使用供应商报告的输入总数，否则使用已知的未缓存输入、缓存读取和缓存写入分桶。', '条执行记录')
        add('cache_read_tokens', '缓存命中 Token', all_usage['cache_read_tokens'], 'Token',
            '统计模型回执记录的缓存读取 Token。', '条执行记录')
        add('output_tokens', '输出 Token', all_usage['output_tokens'], 'Token',
            '统计模型回执记录的输出 Token。', '条执行记录')
        add('cost_usd', '花费（美元）', all_usage['cost_usd'], 'USD',
            '汇总服务已报告的费用估算，结算以账单为准。', '条执行记录')
        return {'schema': 'session-observability/v1', 'generated_at': now(), 'metrics': metrics, 'sessions': sessions,
            'coverage': {'scope': 'self', 'total_sessions': count, 'sessions_returned': len(sessions),
                'sessions_truncated': count > len(sessions), 'total_runs': total, 'runs_included': len(rows),
                'runs_truncated': incomplete,
                'known_request_count': all_usage['request_count'].get('known_value', all_usage['request_count']['value']) or 0,
                'unknown_request_records': total - sum(sample['full']['request_count'] is not None for sample in usage_by_run.values()),
                'missing_usage_records': total - sum(sample['reported'] for sample in usage_by_run.values()),
                'compression_events_truncated': events_truncated,
                'note': '仅当前账号的会话执行回执；未归属会话的 Tab、选模等辅助调用不并入。最多投影最近 20000 条执行记录和 1000 个会话；截断时总量保持未知。'}}


def _cursor(value, conversation_id, user_id):
    if not value:
        return {'until': now(), 'at': '', 'key': ''}
    try:
        if len(value) > 1024:
            raise ValueError()
        decoded = json.loads(base64.urlsafe_b64decode(value + '=' * (-len(value) % 4)))
        if (not isinstance(decoded, dict) or set(decoded) != {'v', 'conversation', 'user', 'until', 'at', 'key'}
                or decoded['v'] != 1 or decoded['conversation'] != conversation_id or decoded['user'] != user_id
                or any(not isinstance(decoded[key], str) for key in ('until', 'at', 'key'))
                or len(decoded['until']) > 64 or len(decoded['at']) > 64 or len(decoded['key']) > 256):
            raise ValueError()
        return decoded
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        raise AppError(422, 'INVALID_CURSOR', '轨迹游标无效，请重新加载') from None


def _safe_identity(value):
    return value if isinstance(value, str) and len(value) <= 180 and all(c.isalnum() or c in '_.:-' for c in value) else None


def trajectory(service, user, conversation_id, *, cursor=None, limit=50):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise AppError(422, 'INVALID_LIMIT', '每页轨迹条数须为 1 到 100')
    with service.db.lock:
        conversation = service.require('conversations', conversation_id, user)
        position = _cursor(cursor, conversation_id, user['id'])
        # No raw_events, SDK files, reasoning blocks or arbitrary tool responses.
        union = """SELECT 'input' kind,m.id identity,m.run_id,m.created_at at,'0:'||m.id sort_key,
                    substr(m.text,1,4001) text,'received' status,'{}' data
                FROM messages m WHERE m.conversation_id=? AND m.role='user'
                UNION ALL
                SELECT 'model',r.id,r.id,r.created_at,'1:'||r.id,
                    CASE WHEN r.status IN ('succeeded','failed','cancelled','interrupted') THEN substr(COALESCE(m.text,''),1,4001) ELSE '' END,
                    r.status,'{}'
                FROM runs r LEFT JOIN messages m ON m.id=r.assistant_message_id AND m.conversation_id=r.conversation_id AND m.role='assistant'
                WHERE r.conversation_id=? AND r.user_id=?
                UNION ALL
                SELECT 'tool',r.id||':'||e.id,r.id,e.created_at,'2:'||r.id||':'||printf('%012d',e.id),'',e.type,substr(e.data,1,64000)
                FROM events e JOIN runs r ON r.id=e.run_id WHERE r.conversation_id=? AND r.user_id=?
                    AND e.type IN ('tool.started','tool.completed')"""
        values = [conversation_id, conversation_id, user['id'], conversation_id, user['id']]
        records = service.db.all('SELECT * FROM (' + union + ') WHERE at<=? AND (at>? OR (at=? AND sort_key>?)) '
            'ORDER BY at,sort_key LIMIT ?', (*values, position['until'], position['at'], position['at'], position['key'], limit + 1))
        has_more = len(records) > limit
        items = []
        public_labels = {item['id']: item['label'] for item in service.catalog.public()}
        for row in records[:limit]:
            details = {}
            text = row['text']
            title = '用户输入'
            status = row['status']
            if row['kind'] == 'model':
                run = service.require('runs', row['run_id'], user)
                metadata = _object(run.get('execution_meta'))
                selection = _object(metadata.get('selection'))
                model_id = selection.get('model_id')
                recover_terminal_usage(service.db, [run])
                usage = _usage([_run_usage(run)], 1)
                details = {'model_id': model_id if model_id in public_labels else None,
                    'model_label': public_labels.get(model_id, '历史模型'), **_values(usage, USAGE_FIELDS)}
                timings = _object(run.get('timings'))
                details['duration_ms'] = _number(timings.get('total_ms'), integer=False) if run['status'] in TERMINAL else None
                code = _safe_identity(run.get('error_code'))
                if code:
                    details['error_code'] = code
                title = '助手回复' if status == 'succeeded' else '执行未完成' if status in TERMINAL else '正在执行'
                if metadata.get('execution_kind') == 'product_metadata':
                    title = '本地处理（无模型调用）'
                    details['model_id'] = None
                    details['model_label'] = None
            elif row['kind'] == 'tool':
                data = _object(row['data'])
                tool_name = _safe_identity(data.get('tool_name')) or '工具'
                finished = row['status'] == 'tool.completed'
                status = 'succeeded' if finished and data.get('success') is True else 'failed' if finished and data.get('success') is False else 'unknown' if finished else 'running'
                title = tool_name
                details = {'tool_name': tool_name, 'duration_ms': _number(data.get('duration_ms'), integer=False),
                    'source_count': len(data['source_ids']) if isinstance(data.get('source_ids'), list) else None}
                code = _safe_identity(data.get('error_code'))
                if code:
                    details['error_code'] = code
                text = {'succeeded': '工具执行完成。', 'failed': '工具执行失败；已取得的依据与失败状态分别记录。',
                        'unknown': '工具执行结果尚未核验。', 'running': '工具开始执行。'}[status]
            items.append({'id': row['kind'] + ':' + row['identity'], 'kind': row['kind'], 'run_id': row['run_id'],
                'at': row['at'], 'title': title, 'status': status, 'text': text[:MAX_TEXT],
                'text_truncated': len(text) > MAX_TEXT, 'details': details})
        run_rows, total_runs = _runs(service, user['id'], conversation_id)
        usage = _usage([_run_usage(run) for run in run_rows], total_runs)
        counts = service.db.one("SELECT COUNT(*) n FROM messages WHERE conversation_id=? AND role='user'", (conversation_id,))
        tools = service.db.one("SELECT COUNT(*) n,COUNT(DISTINCT r.id||':'||CASE WHEN json_valid(e.data) THEN json_extract(e.data,'$.tool_call_id') END) calls,"
            "SUM(CASE WHEN json_valid(e.data) THEN CASE WHEN json_type(e.data,'$.tool_call_id')='text' AND length(json_extract(e.data,'$.tool_call_id'))>0 THEN 0 ELSE 1 END ELSE 1 END) unknown "
            "FROM events e JOIN runs r ON r.id=e.run_id WHERE r.conversation_id=? AND r.user_id=? AND e.type IN ('tool.started','tool.completed')", (conversation_id, user['id']))
        next_cursor = None
        if has_more:
            last = records[limit - 1]
            next_cursor = base64.urlsafe_b64encode(json.dumps({'v': 1, 'conversation': conversation_id, 'user': user['id'],
                'until': position['until'], 'at': last['at'], 'key': last['sort_key']}, separators=(',', ':')).encode()).decode().rstrip('=')
        return {'schema': 'session-trajectory/v1', 'conversation_id': conversation_id, 'items': items,
            'has_more': has_more, 'next_cursor': next_cursor, 'snapshot_at': position['until'],
            'summary': {'input_count': counts['n'], 'turn_count': counts['n'], 'model_count': total_runs,
                'tool_count': tools['n'], 'tool_call_count': tools['calls'] if not tools['unknown'] else None,
                **_values(usage, ('request_count',)), 'started_at': conversation['created_at'],
                'ended_at': None if conversation['active_run_id'] else conversation['updated_at']},
            'coverage': {'scope': 'self', 'public_events_only': True, 'includes_private_reasoning': False,
                'note': '轨迹只展示用户输入、公开助手文本与工具状态；不读取原始运行日志、隐藏思考、工具凭据或完整响应。请求数独立取自可核验回执。'}}
