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

const contextNumber = (value: number | null | undefined) => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value.toLocaleString('zh-CN') : '未提供';

export function contextStatus(context: NativeContext | null): string {
  if (!context) return '上下文状态尚未读取';
  if (context?.stale_reason === 'route_changed') return '模型配置已变化，旧统计已隔离';
  if (context?.stale_reason === 'request_unobserved') return '尚无当前请求的可用统计';
  if (context?.stale) return '统计快照已过期，请刷新';
  if (context?.status === 'not_measured') return '尚未测量';
  if (context?.source === 'harness_token_meter') return context.precision === 'estimated' && ['available', 'partial'].includes(context.status) ? 'Harness 上下文估算' : '上下文估算暂不可用';
  return context?.status === 'available' ? '原生统计可用' : '原生统计暂不可用';
}

export function contextCanCompact(context: NativeContext | null): boolean {
  return context !== null && (context.manual_compact_available ?? context.source === 'native_runtime');
}

export function contextError(error: unknown): string {
  if (error instanceof ApiError && error.code === 'CONTEXT_BUSY') return '当前对话正在执行或等待审批，结束后再操作上下文。';
  if (error instanceof ApiError && error.code === 'CONTEXT_NOT_READY') return '这个对话还没有可恢复的原生会话。请先完成一轮对话。';
  return errorText(error);
}

/** Only Harness refresh is a local projection read. Never auto-refresh Claude. */
export async function readContextSnapshot(conversationId: string, busy = false, signal?: AbortSignal): Promise<NativeContext> {
  const endpoint = `/conversations/${encodeURIComponent(conversationId)}/context`;
  const cached = await api<NativeContext>(endpoint, { signal });
  if (busy || signal?.aborted || cached.source !== 'harness_token_meter' || cached.error
      || !(cached.status === 'not_measured' || cached.stale_reason === 'snapshot_expired')) return cached;
  try { return await api<NativeContext>(`${endpoint}/refresh`, { method: 'POST', signal }); }
  catch (error) {
    if (signal?.aborted) throw error;
    return { ...cached, error: contextError(error) };
  }
}

export function ContextSummary({ context, models }: { context: NativeContext | null; models: ModelOption[] }) {
  const harness = context?.source === 'harness_token_meter';
  const readable = context && !context.stale && (!harness || (context.precision === 'estimated' && ['available', 'partial'].includes(context.status))) ? context : null;
  return <>
    <div className="context-status"><Gauge size={22}/><div><strong>{contextStatus(context)}</strong><p>{context?.measured_at ? `${context.cached ? '上次统计读取时间（缓存）' : '统计读取时间'} · ${timeText(context.measured_at)}` : '尚无统计读取记录'}</p></div></div>
    {context?.error && <ErrorNotice message={context.error}/>}
    <dl className="context-statistics">
      <div><dt>{harness ? '当前上下文占用（估算 Token）' : '上下文 Token'}</dt><dd>{contextNumber(readable?.total_tokens)}</dd></div>
      <div><dt>{harness ? '已核验上下文容量' : '上下文容量'}</dt><dd>{contextNumber(readable?.max_tokens)}</dd></div>
      {harness ? <div><dt>供应商原始输入计数</dt><dd>{contextNumber(readable?.observed_prompt_tokens)}</dd></div> : <div><dt>原生自动压缩</dt><dd>{context?.auto_compact_enabled === true ? '已启用' : context?.auto_compact_enabled === false ? '未启用' : '未提供'}</dd></div>}
      <div><dt>当前模型</dt><dd>{context?.model ? publicModelLabel(models, context.model) : '未提供'}</dd></div>
    </dl>
    {harness && <p className="section-help">{!readable ? '当前没有可用的估算基线，缺失数值保持未知。' : context?.baseline_kind === 'usage' ? '估算以运行时归一化的用量记录为基线，再估计上下文变化；原始用量完整性尚未核验。' : '这是官方运行时的启发式估算，不代表供应商已核验的输入量。'} 适配器可能将缺失用量记为零，因此供应商原始输入计数保持未知。未核验容量时不显示占用百分比；上下文占用不等于累计 Token 账单。</p>}
    {harness && readable?.breakdown && <details className="context-recent"><summary>查看上下文组成估算</summary><p>系统说明：{contextNumber(readable.breakdown.system_tokens)} · 工具定义：{contextNumber(readable.breakdown.tools_tokens)} · 会话内容：{contextNumber(readable.breakdown.message_tokens)}</p><small>各项使用启发式估算，不应相加作为总占用，也不能据此计算费用。</small></details>}
    {context?.stale_reason === 'route_changed' && <p className="section-help">当前配置与原统计不一致，旧值不会套用到新模型。完成当前配置下的一轮正常对话后再刷新。</p>}
    {harness && context?.status === 'unavailable' && !context.stale && <p className="section-help">当前会话暂没有可用的上下文记录。刷新不会额外调用模型来补造统计。</p>}
  </>;
}

export default function ContextDialog({ conversationId, models, busy, run, onClose, onCompact }: { conversationId: string; models: ModelOption[]; busy: boolean; run: Run | null; onClose: () => void; onCompact: (result: { message: Message; run_id: string }) => void }) {
  const [context, setContext] = useState<NativeContext | null>(null); const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false); const [compacting, setCompacting] = useState(false); const [instructions, setInstructions] = useState(''); const [error, setError] = useState('');
  const mounted = useRef(true); const request = useRef<{ instructions: string; id: string } | null>(null);
  const reading = useRef<AbortController | null>(null);
  const endpoint = `/conversations/${encodeURIComponent(conversationId)}`;
  const read = async (refresh = false) => {
    reading.current?.abort();
    const controller = new AbortController(); reading.current = controller;
    const current = () => mounted.current && reading.current === controller && !controller.signal.aborted;
    setError(''); if (refresh) setRefreshing(true); else setLoading(true);
    const timeout = window.setTimeout(() => {
      if (!current()) return;
      setError('读取上下文超时，请重试。当前没有新增统计。');
      setLoading(false); setRefreshing(false); controller.abort(); reading.current = null;
    }, 30_000);
    try {
      const value = refresh ? await api<NativeContext>(`${endpoint}/context/refresh`, { method: 'POST', signal: controller.signal }) : await readContextSnapshot(conversationId, busy, controller.signal);
      if (current()) setContext(value);
    } catch (error) { if (current()) setError(contextError(error)); }
    finally {
      window.clearTimeout(timeout);
      if (current()) { setLoading(false); setRefreshing(false); reading.current = null; }
    }
  };
  useEffect(() => {
    mounted.current = true; setContext(null); setInstructions(''); request.current = null; void read();
    return () => { mounted.current = false; reading.current?.abort(); reading.current = null; };
  }, [endpoint]);
  const handleError = (e: unknown) => {
    setError(contextError(e));
  };
  const refresh = async () => {
    if (busy || compacting || refreshing || reading.current) return;
    await read(!!context);
  };
  const compact = async () => {
    if (busy || compacting || refreshing || !contextCanCompact(context)) return;
    const trimmed = instructions.trim();
    if (!request.current || request.current.instructions !== trimmed) request.current = { instructions: trimmed, id: clientRequestId() };
    setError(''); setCompacting(true);
    try { const result = await post<{ message: Message; run_id: string }>(`${endpoint}/compact`, { client_request_id: request.current.id, ...(trimmed ? { instructions: trimmed } : {}) }); if (mounted.current) onCompact(result); }
    catch (e) { if (mounted.current) handleError(e); } finally { if (mounted.current) setCompacting(false); }
  };
  const number = contextNumber;
  const locked = busy || refreshing || compacting;
  const harness = context?.source === 'harness_token_meter';
  const canCompact = contextCanCompact(context);
  return <Modal title="上下文" description="查看此对话的上下文记录，区分原生统计、估算和未知值。" onClose={() => { if (!compacting) onClose(); }} wide><div className="modal-body"><ErrorNotice message={error}/>
    {loading ? <Loader text="正在读取上下文状态…"/> : <>
      <ContextSummary context={context} models={models}/>
      <div className="context-refresh"><p>{!context ? '尚未读到上下文状态，可以重新读取。' : harness ? '只读取本地官方统计，不发送新提示、不调用模型。短时间重复刷新复用缓存；读取时间不是供应商样本时间。' : '未知统计不会推算成占用百分比。手动刷新可能向模型服务发起计数请求；短时间重复刷新会复用缓存。'}</p><Button variant="secondary" busy={refreshing} disabled={busy || compacting} onClick={() => void refresh()}><RefreshCw size={15}/>{!context ? '重新读取状态' : harness ? '刷新上下文估算' : '刷新原生统计'}</Button></div>
      {!harness && context?.status === 'unavailable' && <p className="section-help">当前模型线路未提供可用的原生统计，此状态不影响继续聊天。</p>}
      <section className="context-compaction"><h3>手动原生压缩</h3>{canCompact ? <><p>交给运行时整理当前会话上下文。此操作可能产生模型用量，历史聊天记录和附件仍会保留。</p><Field label="保留重点（可选）" hint="例如需要继续追踪的目标、已确认结论或待办事项。"><textarea rows={3} value={instructions} onChange={event => setInstructions(event.target.value)} disabled={locked} maxLength={2000} placeholder="希望压缩后继续保留哪些重要信息？"/></Field>{busy && <p className="context-busy-note">对话正在执行或等待审批，结束后才能刷新或压缩。</p>}<Button busy={compacting} disabled={busy || refreshing} onClick={() => void compact()}><Minimize2 size={15}/>开始原生压缩</Button></> : <p>{harness ? '此会话还没有完成授权核验的原生历史，或当前任务仍在执行；完成一轮对话并空闲后再压缩。' : '尚未确认此会话的压缩能力，请刷新状态；当前不会启动整理或生成摘要。'}</p>}</section>
      <section className="context-recent"><h3>最近压缩记录</h3>{context?.last_compaction ? <><p>{compactionStatusLabel(context.last_compaction.status, context.last_compaction.followup_status)}</p><small>{timeText(context.last_compaction.created_at)}{context.last_compaction.pre_tokens != null ? ` · 压缩前 Token：${number(context.last_compaction.pre_tokens)}` : ''}</small>{context.last_compaction.error && <p>{context.last_compaction.error}</p>}</> : run?.kind === 'compact' ? <p>{compactionLabel(run)}</p> : <p>没有已记录的原生压缩结果。</p>}</section>
    </>}
  </div><footer className="modal-footer"><Button variant="secondary" disabled={compacting} onClick={onClose}>关闭</Button></footer></Modal>;
}
