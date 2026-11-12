import { useEffect, useMemo, useState } from 'react';
import { Check, ChevronDown, FileSpreadsheet, LoaderCircle, Play, Quote, ShieldCheck, Square, X } from 'lucide-react';
import { api, clientRequestId, errorText, post, sizeText, timeText } from './api';
import { Button, ErrorNotice, Field, FilePreview, Loader, Modal } from './components';
import { workflowFormFields, workflowRunTraceStatusLabel } from './WorkflowPage';
import type { Message, StoredFile, Workflow, WorkflowArtifactPreview, WorkflowGraph, WorkflowRun } from './types';

export function workflowChatStatusLabel(status: string): string {
  return ({
    awaiting_approval: '等待你审批',
    succeeded: '已完成',
    failed: '执行失败',
    cancelled: '已取消',
    running: '正在运行',
    queued: '排队中',
  } as Record<string, string>)[status] || status || '未知状态';
}

export function workflowChatReference(run: WorkflowRun): string {
  const name = run.workflow_name || '工作流';
  const outputs = run.output?.node_outputs;
  const lines: string[] = [];
  if (outputs && typeof outputs === 'object') {
    for (const [nodeId, value] of Object.entries(outputs as Record<string, unknown>).slice(0, 4)) {
      const text = String(value ?? '').replace(/\s+/g, ' ').trim();
      if (!text || text.includes('"workflow_approval"')) continue;
      lines.push(`${nodeId}: ${text.slice(0, 160)}`);
    }
  }
  const body = lines.length ? lines.map(line => `> ${line}`).join('\n') : '> 结果已写入当前对话，请结合下方回答继续提问。';
  return `> 引用工作流「${name}」运行 ${run.id.slice(0, 8)}\n${body}\n\n`;
}

export function workflowArtifactFile(artifact: WorkflowArtifactPreview): StoredFile | null {
  if (!artifact.written || !artifact.file_id || !artifact.file_name) return null;
  return {
    id: artifact.file_id,
    name: artifact.file_name,
    mime_type: artifact.mime_type || 'application/octet-stream',
    size: artifact.size || 0,
    created_at: '',
  };
}

export function workflowRunIssue(fields: Array<{ id: string; label: string; required: boolean }>, input: Record<string, unknown>): string {
  const missing = fields.filter(field => field.required && (input[field.id] === undefined || input[field.id] === null || input[field.id] === ''));
  return missing.length ? `请填写必填字段：${missing.map(field => field.label).join('、')}` : '';
}

export function WorkflowRunDialog({ assistantId, conversationId, onClose, onStarted }: { assistantId: string; conversationId: string; onClose: () => void; onStarted: (message: Message) => void }) {
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [selectedId, setSelectedId] = useState('');
  const [detail, setDetail] = useState<Workflow | null>(null);
  const [input, setInput] = useState<Record<string, unknown>>({});
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    let current = true;
    api<Workflow[]>(`/assistants/${encodeURIComponent(assistantId)}/workflows`)
      .then(values => {
        if (!current) return;
        const published = values.filter(item => item.published_version !== null);
        setWorkflows(published);
        setSelectedId(published[0]?.id || '');
      })
      .catch(cause => { if (current) setError(errorText(cause)); })
      .finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [assistantId]);

  useEffect(() => {
    if (!selectedId) { setDetail(null); return; }
    let current = true;
    api<Workflow>(`/workflows/${encodeURIComponent(selectedId)}`)
      .then(value => {
        if (!current) return;
        setDetail(value);
        setInput({});
      })
      .catch(cause => { if (current) setError(errorText(cause)); });
    return () => { current = false; };
  }, [selectedId]);

  const graph: WorkflowGraph | null = useMemo(() => {
    if (!detail || detail.published_version === null) return null;
    return detail.versions.find(version => version.version === detail.published_version)?.graph || null;
  }, [detail]);
  const fields = useMemo(() => graph ? workflowFormFields(graph) : [], [graph]);
  const request = useMemo(() => clientRequestId(), [conversationId, selectedId]);

  const submit = async () => {
    if (!detail || !graph) { setError('请选择一个已发布的工作流。'); return; }
    const issue = workflowRunIssue(fields, input);
    if (issue) { setError(issue); return; }
    setBusy(true); setError('');
    try {
      const result = await post<{ message: Message; assistant_message: Message; run_id: string; run: WorkflowRun }>(`/conversations/${encodeURIComponent(conversationId)}/workflows/${encodeURIComponent(detail.id)}/runs`, { client_request_id: request, input });
      onStarted(result.assistant_message);
    } catch (cause) { setError(errorText(cause)); }
    finally { setBusy(false); }
  };

  return <Modal title="在聊天中运行工作流" description="只运行已发布版本；审批、取消和产物都留在当前对话。" wide onClose={() => { if (!busy) onClose(); }}>
    <div className="modal-body">
      <ErrorNotice message={error} onDismiss={() => setError('')}/>
      {loading ? <Loader text="正在读取已发布工作流…"/> : !workflows.length ? <p className="section-help">此助手还没有已发布的工作流。请先在“工作流”页面发布一个版本。</p> : <>
        <Field label="工作流" hint="运行会固定到当前发布版本，不会使用草稿。">
          <select value={selectedId} disabled={busy} onChange={event => setSelectedId(event.target.value)}>
            {workflows.map(workflow => <option key={workflow.id} value={workflow.id}>{workflow.name} · v{workflow.published_version}</option>)}
          </select>
        </Field>
        {detail && <div className="workflow-chat-meta" role="status"><span>版本 v{detail.published_version}</span><span>{detail.synthetic_only ? '合成/工具执行，不伪装模型结果' : '按节点配置执行'}</span><span>审批与取消均在聊天内完成</span></div>}
        {fields.length > 0 ? <div className="workflow-chat-form">{fields.map(field => <Field key={field.id} label={field.label} required={field.required}>
          {field.type === 'choice' ? <select aria-label={field.label} value={String(input[field.id] ?? '')} disabled={busy} onChange={event => setInput(current => ({ ...current, [field.id]: event.target.value }))}><option value="">请选择</option>{(field.options || []).map(option => <option key={option} value={option}>{option}</option>)}</select> : <input aria-label={field.label} type={field.type === 'number' ? 'number' : field.type === 'date' ? 'date' : 'text'} value={String(input[field.id] ?? '')} disabled={busy} onChange={event => setInput(current => ({ ...current, [field.id]: field.type === 'number' ? (event.target.value === '' ? '' : Number(event.target.value)) : event.target.value }))}/>}
        </Field>)}</div> : <p className="section-help">这个工作流没有声明输入字段，可以直接运行。</p>}
      </>}
    </div>
    <footer className="modal-footer"><Button variant="secondary" disabled={busy} onClick={onClose}>取消</Button><Button busy={busy} disabled={!workflows.length || !detail} onClick={() => void submit()}><Play size={14}/>运行工作流</Button></footer>
  </Modal>;
}

export function WorkflowChatCard({ runId, onReload, onReference }: { runId: string; onReload: () => void; onReference: (run: WorkflowRun) => void }) {
  const [run, setRun] = useState<WorkflowRun | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [comment, setComment] = useState('');
  const [preview, setPreview] = useState<StoredFile | null>(null);
  const [expanded, setExpanded] = useState(false);

  const load = async () => {
    try {
      const value = await api<WorkflowRun>(`/workflow-runs/${encodeURIComponent(runId)}`);
      setRun(value);
      setError('');
    } catch (cause) { setError(errorText(cause)); }
  };
  useEffect(() => { void load(); }, [runId]); // eslint-disable-line react-hooks/exhaustive-deps

  const pending = run?.approvals?.find(item => item.status === 'pending') || (run?.approval?.status === 'pending' ? run.approval : null);
  const artifacts = (run?.output?.artifacts || []).map(workflowArtifactFile).filter((item): item is StoredFile => !!item);

  const decide = async (decision: 'approve' | 'reject') => {
    if (!run || !pending) return;
    setBusy(true); setError('');
    try {
      const next = await post<WorkflowRun>(`/workflow-runs/${encodeURIComponent(run.id)}/approvals/${encodeURIComponent(pending.node_id)}/decision`, { decision, comment: comment.trim() });
      setRun(next); setComment(''); onReload();
    } catch (cause) { setError(errorText(cause)); }
    finally { setBusy(false); }
  };
  const cancel = async () => {
    if (!run) return;
    setBusy(true); setError('');
    try {
      const next = await post<WorkflowRun>(`/workflow-runs/${encodeURIComponent(run.id)}/cancel`);
      setRun(next); onReload();
    } catch (cause) { setError(errorText(cause)); }
    finally { setBusy(false); }
  };

  if (!run && !error) return <div className="workflow-chat-card loading"><LoaderCircle className="spin" size={16}/>正在读取工作流状态…</div>;
  return <section className={`workflow-chat-card ${run?.status || 'failed'}`} aria-label="聊天内工作流">
    <header>
      <div><span className="eyebrow">聊天内工作流</span><strong>{run?.workflow_name || run?.workflow_id || '工作流'}</strong></div>
      <span className={`badge ${run?.status === 'succeeded' ? 'green' : run?.status === 'failed' ? 'amber' : ''}`}>{run ? workflowChatStatusLabel(run.status) : '状态不可用'}</span>
    </header>
    {error && <ErrorNotice message={error} onDismiss={() => setError('')}/>}
    {run && <>
      <p className="workflow-chat-summary">运行 {run.id.slice(0, 8)} · 版本 v{run.version} · {run.execution_mode === 'model' ? '模型节点' : run.execution_mode === 'tool' ? '实时只读工具' : '合成执行'}{run.completed_at ? ` · ${timeText(run.completed_at)}完成` : ''}</p>
      {run.error && (run.status === 'failed' ? <p className="workflow-chat-error">{run.error}</p> : <p className="workflow-chat-summary">{run.error}</p>)}
      <button type="button" className="workflow-chat-trace-toggle" aria-expanded={expanded} onClick={() => setExpanded(value => !value)}><ChevronDown size={14}/>{expanded ? '收起节点记录' : `查看 ${run.trace.length} 个节点记录`}</button>
      {expanded && <ol className="workflow-chat-trace">{run.trace.map(item => <li key={`${item.sequence}-${item.node_id}`} className={item.status}><span>{item.node_id}</span><small>{workflowRunTraceStatusLabel(item.status)}{item.condition_result === true ? ' · 条件为真' : item.condition_result === false ? ' · 条件为假' : ''}{item.tool_name ? ` · ${item.tool_name}` : ''}</small></li>)}</ol>}
      {artifacts.length > 0 && <div className="workflow-chat-artifacts"><span className="eyebrow">工作流产物</span>{artifacts.map(file => <button key={file.id} type="button" className="file-chip-main" onClick={() => setPreview(file)}><FileSpreadsheet size={16}/><span>{file.name}<small>{sizeText(file.size)}</small></span></button>)}</div>}
      {pending && run.status === 'awaiting_approval' && <div className="workflow-chat-approval" role="group" aria-label="工作流审批"><p><ShieldCheck size={15}/>审批节点「{pending.node_id}」正在等待明确决定。</p><input aria-label="审批说明" placeholder="审批说明（可选）" value={comment} maxLength={1000} disabled={busy} onChange={event => setComment(event.target.value)}/><div className="workflow-chat-actions"><Button variant="secondary" disabled={busy} onClick={() => void decide('reject')}><X size={14}/>拒绝</Button><Button busy={busy} onClick={() => void decide('approve')}><Check size={14}/>批准并继续</Button></div></div>}
      {!pending && run.status === 'awaiting_approval' && <div className="workflow-chat-actions"><Button variant="secondary" disabled={busy} onClick={() => void cancel()}><Square size={13}/>取消运行</Button></div>}
      <div className="workflow-chat-actions"><Button variant="ghost" disabled={busy} onClick={() => onReference(run)}><Quote size={14}/>引用此结果</Button>{pending && <Button variant="ghost" disabled={busy} onClick={() => void cancel()}><Square size={13}/>取消运行</Button>}</div>
    </>}
    {preview && <FilePreview key={preview.id} file={preview} onClose={() => setPreview(null)}/>}
  </section>;
}
