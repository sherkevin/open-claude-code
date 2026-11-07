import { useEffect, useRef, useState } from 'react';
import { Gauge, Minimize2, RefreshCw } from 'lucide-react';
import { api, ApiError, clientRequestId, errorText, post, timeText } from './api';
import { Button, ErrorNotice, Field, Loader, Modal } from './components';
import type { Message, ModelOption, NativeContext, Run } from './types';
import { compactionStatusLabel } from './display';
import { publicModelLabel } from './models';

export function compactionLabel(run: Run): string {
  if (run.compaction?.status === 'completed') return compactionStatusLabel('completed', run.compaction.followup_status || (['failed', 'cancelled', 'interrupted'].includes(run.status) ? run.status : null));
  if (['queued', 'running', 'awaiting_approval', 'cancelling'].includes(run.status)) return run.status === 'cancelling' ? '正在停止压缩' : '正在执行原生压缩';
  if (run.status !== 'succeeded') return compactionStatusLabel(run.status);
  return compactionStatusLabel(run.compaction?.status);
}

export default function ContextDialog({ conversationId, models, busy, run, onClose, onCompact }: { conversationId: string; models: ModelOption[]; busy: boolean; run: Run | null; onClose: () => void; onCompact: (result: { message: Message; run_id: string }) => void }) {
  const [context, setContext] = useState<NativeContext | null>(null); const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false); const [compacting, setCompacting] = useState(false); const [instructions, setInstructions] = useState(''); const [error, setError] = useState('');
  const mounted = useRef(true); const request = useRef<{ instructions: string; id: string } | null>(null);
  const endpoint = `/conversations/${encodeURIComponent(conversationId)}`;
  useEffect(() => {
    mounted.current = true;
    api<NativeContext>(`${endpoint}/context`).then(value => { if (mounted.current) setContext(value); }).catch(e => { if (mounted.current) setError(errorText(e)); }).finally(() => { if (mounted.current) setLoading(false); });
    return () => { mounted.current = false; };
  }, [endpoint]);
  const handleError = (e: unknown) => {
    if (e instanceof ApiError && e.code === 'CONTEXT_BUSY') setError('当前对话正在执行或等待审批，结束后再操作上下文。');
    else if (e instanceof ApiError && e.code === 'CONTEXT_NOT_READY') setError('这个对话还没有可恢复的原生会话。请先完成一轮对话。');
    else setError(errorText(e));
  };
  const refresh = async () => {
    setError(''); setRefreshing(true);
    try { const value = await post<NativeContext>(`${endpoint}/context/refresh`); if (mounted.current) setContext(value); }
    catch (e) { if (mounted.current) handleError(e); } finally { if (mounted.current) setRefreshing(false); }
  };
  const compact = async () => {
    if (busy || compacting || refreshing) return;
    const trimmed = instructions.trim();
    if (!request.current || request.current.instructions !== trimmed) request.current = { instructions: trimmed, id: clientRequestId() };
    setError(''); setCompacting(true);
    try { const result = await post<{ message: Message; run_id: string }>(`${endpoint}/compact`, { client_request_id: request.current.id, ...(trimmed ? { instructions: trimmed } : {}) }); if (mounted.current) onCompact(result); }
    catch (e) { if (mounted.current) handleError(e); } finally { if (mounted.current) setCompacting(false); }
  };
  const number = (value: number | null | undefined) => typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString('zh-CN') : '未提供';
  const locked = busy || refreshing || compacting;
  return <Modal title="上下文" description="查看此对话的原生上下文统计，并在需要时手动压缩。" onClose={() => { if (!compacting) onClose(); }} wide><div className="modal-body"><ErrorNotice message={error}/>
    {loading ? <Loader text="正在读取上下文状态…"/> : <>
      <div className="context-status"><Gauge size={22}/><div><strong>{context?.status === 'available' ? '原生统计可用' : context?.status === 'not_measured' ? '尚未测量' : '原生统计暂不可用'}</strong><p>{context?.measured_at ? `${context.cached ? '缓存统计' : '最近测量'} · ${timeText(context.measured_at)}` : '未提供测量时间'}</p></div></div>
      <dl className="context-statistics"><div><dt>上下文 Token</dt><dd>{number(context?.total_tokens)}</dd></div><div><dt>上下文容量</dt><dd>{number(context?.max_tokens)}</dd></div><div><dt>原生自动压缩</dt><dd>{context?.auto_compact_enabled === true ? '已启用' : context?.auto_compact_enabled === false ? '未启用' : '未提供'}</dd></div><div><dt>模型</dt><dd>{context?.model ? publicModelLabel(models, context.model) : '未提供'}</dd></div></dl>
      <div className="context-refresh"><p>未知统计不会推算成占用百分比。手动刷新可能向模型服务发起计数请求；短时间重复刷新会复用缓存。</p><Button variant="secondary" busy={refreshing} disabled={busy || compacting} onClick={() => void refresh()}><RefreshCw size={15}/>刷新原生统计</Button></div>
      {context?.status === 'unavailable' && <p className="section-help">当前模型线路未提供可用的原生统计，此状态不影响继续聊天。</p>}
      <section className="context-compaction"><h3>手动原生压缩</h3><p>交给运行时整理当前会话上下文。此操作可能产生模型用量，历史聊天记录和附件仍会保留。</p><Field label="保留重点（可选）" hint="例如需要继续追踪的目标、已确认结论或待办事项。"><textarea rows={3} value={instructions} onChange={event => setInstructions(event.target.value)} disabled={locked} maxLength={2000} placeholder="希望压缩后继续保留哪些重要信息？"/></Field>{busy && <p className="context-busy-note">对话正在执行或等待审批，结束后才能刷新或压缩。</p>}<Button busy={compacting} disabled={busy || refreshing} onClick={() => void compact()}><Minimize2 size={15}/>开始原生压缩</Button></section>
      <section className="context-recent"><h3>最近压缩记录</h3>{context?.last_compaction ? <><p>{compactionStatusLabel(context.last_compaction.status, context.last_compaction.followup_status)}</p><small>{timeText(context.last_compaction.created_at)}{context.last_compaction.pre_tokens != null ? ` · 压缩前 Token：${number(context.last_compaction.pre_tokens)}` : ''}</small>{context.last_compaction.error && <p>{context.last_compaction.error}</p>}</> : run?.kind === 'compact' ? <p>{compactionLabel(run)}</p> : <p>没有已记录的原生压缩结果。</p>}</section>
    </>}
  </div><footer className="modal-footer"><Button variant="secondary" disabled={compacting} onClick={onClose}>关闭</Button></footer></Modal>;
}
