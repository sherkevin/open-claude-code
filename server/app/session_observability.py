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
from .usage_accounting import _aggregate, _entry, _object

MAX_SESSIONS = 1000
MAX_RUNS = 20000
MAX_COMPACTION_EVENTS = 20000
MAX_TEXT = 4000
TERMINAL = frozenset({'succeeded', 'failed', 'cancelled', 'interrupted'})
TOKEN_NAMES = {'input_tokens': 'input_tokens', 'output_tokens': 'output_tokens',
               'cache_read_tokens': 'cache_read_input_tokens', 'cache_write_tokens': 'cache_creation_input_tokens'}


def _number(value, *, integer=True):
    valid = type(value) is int if integer else type(value) in (int, float)
    return value if valid and math.isfinite(value) and 0 <= value <= 10**15 else None


def _precision(value, known=0, *, estimated=False):
    return ('estimated' if estimated else 'observed') if value is not None else 'partial' if known else 'unavailable'


def _runs(service, user_id, conversation_id=None):
    where = 'c.user_id=? AND r.user_id=?'
    values = [user_id, user_id]
    if conversation_id is not None:
        where += ' AND c.id=?'
        values.append(conversation_id)
    sql = ' FROM runs r JOIN conversations c ON c.id=r.conversation_id WHERE ' + where
    total = service.db.one('SELECT COUNT(*) n' + sql, values)['n']
    rows = service.db.all('SELECT r.*' + sql + ' ORDER BY r.created_at DESC,r.id DESC LIMIT ?', (*values, MAX_RUNS))
    return rows, total


def _usage(rows, total):
    result = _aggregate([_entry(row) for row in rows])
    if len(rows) != total:
        for key in (*TOKEN_NAMES.values(), 'estimated_cost_usd', 'model_call_count'):
            result[key] = None
    return result


def _total_input(usage):
    buckets = [usage[field] for field in ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')]
    return sum(buckets) if all(value is not None for value in buckets) else None


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
    before, after = _object(record.get('before')), _object(record.get('after'))
    first, first_precision = _context_value(before)
    last, last_precision = _context_value(after)
    if (first is None or last is None or before.get('source') != after.get('source')
            or before.get('model') != after.get('model')
            or before.get('_binding') != after.get('_binding')):
        return None, 'unavailable'
    return first - last, 'estimated' if 'estimated' in {first_precision, last_precision} else 'observed'


def _compressions(service, rows):
    events = defaultdict(list)
    truncated = False
    ids = [row['id'] for row in rows]
    remaining = MAX_COMPACTION_EVENTS
    for offset in range(0, len(ids), 400):
        batch = ids[offset:offset + 400]
        if remaining == 0:
            truncated = True
            break
        found = service.db.all('SELECT run_id,type,data FROM events WHERE run_id IN (' + ','.join('?' for _ in batch) +
            ") AND type IN ('context.compacted','context.compaction_result') ORDER BY created_at,id LIMIT ?", (*batch, remaining + 1))
        if len(found) > remaining:
            truncated = True
        for event in found[:remaining]:
            events[event['run_id']].append(event)
        remaining -= min(len(found), remaining)
    result = defaultdict(list)
    for row in rows:
        record = _object(row.get('compaction'))
        boundaries = [_object(event['data']) for event in events[row['id']] if event['type'] == 'context.compacted']
        finished = [_object(event['data']) for event in events[row['id']] if event['type'] == 'context.compaction_result'
                    and _object(event['data']).get('status') == 'completed']
        if record.get('status') == 'completed' and (record.get('boundary_observed') is True or boundaries):
            # A compacted boundary and its final result describe one operation.
            records = boundaries[:-1] + [record] if boundaries else [record]
        elif finished and (finished[-1].get('boundary_observed') is True or boundaries):
            records = boundaries[:-1] + [finished[-1]] if boundaries else [finished[-1]]
        else:
            records = boundaries
        result[row['conversation_id']].extend(_compression_difference(item) for item in records)
    return result, truncated


def _compression_summary(records, incomplete=False):
    known = [value for value, _ in records if value is not None]
    value = sum(known) if records and len(known) == len(records) and not incomplete else None
    precision = _precision(value, len(known), estimated=any(kind == 'estimated' for _, kind in records))
    if incomplete:
        precision = 'partial'
    return {'compression_count': len(records), 'compression_saved_tokens': value,
            'compression_precision': precision, 'net_saved_tokens': None, 'compression_return_usd': None}


def overview(service, user):
    """Include archived conversations, but never another user's conversations."""
    with service.db.lock:
        count = service.db.one('SELECT COUNT(*) n FROM conversations WHERE user_id=?', (user['id'],))['n']
        conversations = service.db.all('SELECT id,title,updated_at,archived,active_run_id FROM conversations WHERE user_id=? '
            'ORDER BY updated_at DESC,id DESC LIMIT ?', (user['id'], MAX_SESSIONS))
        rows, total = _runs(service, user['id'])
        all_usage = _usage(rows, total)
        by_session = defaultdict(list)
        for row in rows:
            by_session[row['conversation_id']].append(row)
        run_counts = {row['conversation_id']: row['n'] for row in service.db.all(
            'SELECT r.conversation_id,COUNT(*) n FROM runs r JOIN conversations c ON c.id=r.conversation_id '
            'WHERE c.user_id=? AND r.user_id=? GROUP BY r.conversation_id', (user['id'], user['id']))}
        compression, events_truncated = _compressions(service, rows)
        incomplete = total != len(rows)
        saved = _compression_summary([item for values in compression.values() for item in values], incomplete or events_truncated)
        sessions = []
        for conversation in conversations:
            session_rows = by_session[conversation['id']]
            run_count = run_counts.get(conversation['id'], 0)
            usage = _usage(session_rows, run_count)
            tokens = {key: usage[field] for key, field in TOKEN_NAMES.items()}
            known = sum(usage['known_subtotals'][field] for field in TOKEN_NAMES.values())
            sessions.append({**{key: conversation[key] for key in ('id', 'title', 'updated_at')},
                'status': 'running' if conversation['active_run_id'] else 'archived' if conversation['archived'] else 'idle',
                'run_count': run_count, 'request_count': usage['model_call_count'], **tokens,
                'total_input_tokens': _total_input(usage),
                'token_precision': 'observed' if all(value is not None for value in tokens.values()) else _precision(None, known),
                'request_precision': _precision(usage['model_call_count'], usage['known_model_calls']),
                'cost_usd': usage['estimated_cost_usd'], 'cost_precision': _precision(usage['estimated_cost_usd'], estimated=True),
                **_compression_summary(compression[conversation['id']], run_count != len(session_rows) or events_truncated)})
        metrics = []
        def add(identity, title, value, unit, precision, note):
            metrics.append({'id': identity, 'title': title, 'value': value, 'unit': unit, 'precision': precision, 'note': note})
        add('session_count', '会话总数', count, '个', 'observed', '仅当前账号，包含已归档会话。')
        add('request_count', '模型请求总数', all_usage['model_call_count'], '次',
            _precision(all_usage['model_call_count'], all_usage['known_model_calls']),
            f"已核验 {all_usage['known_model_calls']} 次；{all_usage['unknown_call_records']} 条执行记录未报告完整调用次数。SDK 汇总回执不等于一次请求。")
        add('compression_saved_tokens', '压缩上下文减少', saved['compression_saved_tokens'], 'Token', saved['compression_precision'],
            '仅累计可核验的压缩前后上下文差额，估算另行标注；不代表累计请求节省或账单节省。无完整前后回执时保持未知。')
        add('net_saved_tokens', '净节省', None, 'Token', 'unavailable', '尚无可核验的摘要与上下文重建成本回执，不能计算净节省。')
        add('compression_return_usd', '压缩回本', None, 'USD', 'unavailable', '尚无完整压缩成本与价格依据，不换算美元收益。')
        denominator = _total_input(all_usage)
        rate = all_usage['cache_read_input_tokens'] / denominator if denominator is not None and denominator > 0 else None
        add('cache_hit_rate', '缓存命中率', rate, 'ratio', _precision(rate),
            '缓存读取 /（未缓存输入 + 缓存读取 + 缓存写入）；缺任一计数或分母为零时不估算。')
        add('input_tokens', '输入 Token', denominator, 'Token',
            _precision(denominator, sum(all_usage['known_subtotals'][field] for field in ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'))),
            '总输入 = 未缓存输入 + 缓存读取 + 缓存写入；任一计数缺失时保持未知。')
        for identity, title, field in [('cache_read_tokens', '缓存命中 Token', 'cache_read_input_tokens'), ('output_tokens', '输出 Token', 'output_tokens')]:
            add(identity, title, all_usage[field], 'Token', _precision(all_usage[field], all_usage['known_subtotals'][field]),
                '按服务已记录的模型用量汇总。' +
                f"缺失回执不补零；已知小计 {all_usage['known_subtotals'][field]}。")
        add('cost_usd', '花费（美元）', all_usage['estimated_cost_usd'], 'USD', _precision(all_usage['estimated_cost_usd'], estimated=True),
            '仅服务返回的费用估算，不代表结算账单；未返回费用时保持未知。')
        return {'schema': 'session-observability/v1', 'generated_at': now(), 'metrics': metrics, 'sessions': sessions,
            'coverage': {'scope': 'self', 'total_sessions': count, 'sessions_returned': len(sessions),
                'sessions_truncated': count > len(sessions), 'total_runs': total, 'runs_included': len(rows),
                'runs_truncated': incomplete, 'known_request_count': all_usage['known_model_calls'],
                'unknown_request_records': all_usage['unknown_call_records'] + total - len(rows),
                'missing_usage_records': all_usage['missing_usage_runs'] + total - len(rows),
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
                usage = _usage([run], 1)
                details = {'model_id': model_id if model_id in public_labels else None,
                    'model_label': public_labels.get(model_id, '历史模型'), 'request_count': usage['model_call_count'],
                    'total_input_tokens': _total_input(usage),
                    **{key: usage[field] for key, field in TOKEN_NAMES.items()}}
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
        usage = _usage(run_rows, total_runs)
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
                'request_count': usage['model_call_count'], 'started_at': conversation['created_at'],
                'ended_at': None if conversation['active_run_id'] else conversation['updated_at']},
            'coverage': {'scope': 'self', 'public_events_only': True, 'includes_private_reasoning': False,
                'note': '轨迹只展示用户输入、公开助手文本与工具状态；不读取原始运行日志、隐藏思考、工具凭据或完整响应。请求数独立取自可核验回执。'}}
