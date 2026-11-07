import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react';
import { Check, ChevronRight, CircleDot, GitBranch, Play, Plus, RotateCcw, Save, ShieldCheck, Trash2, Upload } from 'lucide-react';
import { api, errorText, patch, post } from './api';
import { Button, ErrorNotice, Field, Loader, PageHeader, SuccessNotice } from './components';
import type { Assistant, ModelOption, Workflow, WorkflowApproval, WorkflowGraph, WorkflowNode, WorkflowNodeType, WorkflowRun, WorkflowTemplate } from './types';
import { workflowGraphHasCycle } from './workflowFlowgram';

const FlowGramWorkflowCanvas = lazy(() => import('./FlowGramWorkflowCanvas'));

export const DEFAULT_WORKFLOW_GRAPH: WorkflowGraph = {
  nodes: [
    { id: 'agent', type: 'agent', label: '拆解业务目标', config: { mode: 'synthetic' } },
    { id: 'knowledge', type: 'knowledge_search', label: '检索公司知识', config: { mode: 'synthetic' } },
    { id: 'approval', type: 'approval', label: '人工确认', config: {} },
  ],
  edges: [
    { source: 'agent', target: 'knowledge', label: '需要依据' },
    { source: 'knowledge', target: 'approval', label: '提交审批' },
  ],
};

const NODE_META: Record<WorkflowNodeType, { label: string; hint: string; color: string }> = {
  agent: { label: '智能体节点', hint: '受控的任务执行单元', color: '#668e55' },
  skill: { label: '工作方法', hint: '复用文本工作方法', color: '#8b9b64' },
  knowledge_search: { label: '知识检索', hint: '检索已授权知识库', color: '#527d8f' },
  mcp_read: { label: '只读工具', hint: '仅读取已授权业务数据', color: '#8b7651' },
  condition: { label: '条件分支', hint: '按业务条件选择路径', color: '#8d6b8f' },
  approval: { label: '人工审批', hint: '暂停并等待明确决定', color: '#b07c4f' },
  artifact: { label: '产物输出', hint: '生成结构化结果或文件', color: '#557e75' },
};

export function topologicalOrder(graph: WorkflowGraph): string[] {
  const indegree = new Map(graph.nodes.map(node => [node.id, 0]));
  const next = new Map<string, string[]>();
  graph.nodes.forEach(node => next.set(node.id, []));
  graph.edges.forEach(edge => { if (indegree.has(edge.target) && next.has(edge.source)) { indegree.set(edge.target, (indegree.get(edge.target) || 0) + 1); next.get(edge.source)?.push(edge.target); } });
  const queue = [...indegree.entries()].filter(([, value]) => value === 0).map(([id]) => id).sort();
  const result: string[] = [];
  while (queue.length) { const id = queue.shift()!; result.push(id); for (const target of (next.get(id) || []).sort()) { indegree.set(target, (indegree.get(target) || 0) - 1); if (indegree.get(target) === 0) queue.push(target); } }
  return result;
}

export function graphHasCycle(graph: WorkflowGraph) { return workflowGraphHasCycle(graph); }
export function graphHasIsolatedNode(graph: WorkflowGraph) {
  if (graph.nodes.length <= 1) return false;
  const connected = new Set<string>();
  graph.edges.forEach(edge => { connected.add(edge.source); connected.add(edge.target); });
  return graph.nodes.some(node => !connected.has(node.id));
}
export interface WorkflowFormField { id: string; type: 'text' | 'number' | 'date' | 'choice'; label: string; required: boolean; options?: string[] }
export function workflowFormFields(graph: WorkflowGraph): WorkflowFormField[] {
  const seen = new Set<string>(); const result: WorkflowFormField[] = [];
  graph.nodes.forEach(node => {
    const fields = node.config.form_fields;
    if (!Array.isArray(fields)) return;
    fields.forEach(value => { if (!value || typeof value !== 'object') return; const field = value as Record<string, unknown>; const id = typeof field.id === 'string' ? field.id : ''; if (!id || seen.has(id)) return; const type = field.type === 'number' || field.type === 'date' || field.type === 'choice' ? field.type : 'text'; result.push({ id, type, label: typeof field.label === 'string' ? field.label : id, required: field.required === true, options: Array.isArray(field.options) ? field.options.filter(item => typeof item === 'string') as string[] : undefined }); seen.add(id); });
  });
  return result;
}

function cloneGraph(graph: WorkflowGraph): WorkflowGraph { return { nodes: graph.nodes.map(node => ({ ...node, config: { ...node.config } })), edges: graph.edges.map(edge => ({ ...edge })) }; }
function graphFrom(workflow: Workflow): WorkflowGraph { return cloneGraph(workflow.versions.find(version => version.version === workflow.draft_version)?.graph || DEFAULT_WORKFLOW_GRAPH); }
function nodeId(type: WorkflowNodeType, nodes: WorkflowNode[]) { const stem = type.replace('_', '-'); let index = 1; while (nodes.some(node => node.id === `${stem}-${index}`)) index += 1; return `${stem}-${index}`; }
function nodeConfigText(node: WorkflowNode | undefined, key: string, fallback = '') { const value = node?.config?.[key]; return typeof value === 'string' ? value : fallback; }
function nodeConfigNumber(node: WorkflowNode | undefined, key: string, fallback: number) { const value = node?.config?.[key]; return typeof value === 'number' && Number.isFinite(value) ? value : fallback; }
function nodeConfigValueText(node: WorkflowNode | undefined, key: string, fallback = '') { const value = node?.config?.[key]; return typeof value === 'string' || typeof value === 'number' ? String(value) : fallback; }
function conditionValue(value: string) { if (!value.trim()) return null; if (/^-?(?:\d+\.?\d*|\.\d+)$/.test(value.trim())) return Number(value); return value; }
export interface WorkflowInputBinding { key: string; source: string }
export function workflowInputBindings(node: WorkflowNode | undefined): WorkflowInputBinding[] {
  const value = node?.config?.input_bindings;
  if (!Array.isArray(value)) return [];
  return value.filter(item => !!item && typeof item === 'object' && typeof (item as Record<string, unknown>).key === 'string' && typeof (item as Record<string, unknown>).source === 'string').map(item => ({ key: String((item as Record<string, unknown>).key), source: String((item as Record<string, unknown>).source) }));
}
export function workflowInputBindingsText(node: WorkflowNode | undefined) { return workflowInputBindings(node).map(item => `${item.key} = ${item.source}`).join('\n'); }
export function parseWorkflowInputBindings(value: string): WorkflowInputBinding[] {
  return value.split('\n').map(line => { const separator = line.indexOf('='); if (separator < 0) return { key: line.trim(), source: '' }; return { key: line.slice(0, separator).trim(), source: line.slice(separator + 1).trim() }; }).filter(item => item.key || item.source);
}
export function workflowInputBindingsIssue(graph: WorkflowGraph): string {
  const keyPattern = /^[A-Za-z][A-Za-z0-9_-]{0,63}$/;
  const sourcePattern = /^(?:input(?:\.[A-Za-z][A-Za-z0-9_-]{0,63})?|node\.[A-Za-z0-9_-]{1,64}\.output)$/;
  const positions = new Map(topologicalOrder(graph).map((id, index) => [id, index]));
  for (const node of graph.nodes) {
    if (node.type !== 'agent') continue;
    const seen = new Set<string>();
    for (const binding of workflowInputBindings(node)) {
      if (!keyPattern.test(binding.key) || seen.has(binding.key)) return `不能保存：节点“${node.label || node.id}”的变量名称必须唯一且使用英文标识。`;
      if (!sourcePattern.test(binding.source)) return `不能保存：节点“${node.label || node.id}”的变量来源格式无效。`;
      seen.add(binding.key);
      if (binding.source.startsWith('node.')) {
        const sourceId = binding.source.slice(5, -7);
        if (!positions.has(sourceId)) return `不能保存：节点“${node.label || node.id}”引用的来源节点不存在。`;
        if (sourceId === node.id || (positions.get(sourceId) ?? 0) >= (positions.get(node.id) ?? 0)) return `不能保存：节点“${node.label || node.id}”只能引用已经完成的上游节点。`;
      }
    }
  }
  return '';
}
const workflowKnowledgeTemplate = /\{\{\s*(input(?:\.[A-Za-z][A-Za-z0-9_-]{0,63})?|node\.[A-Za-z0-9_-]{1,64}\.output)\s*\}\}/g;
const workflowKnowledgeSource = /^(?:input(?:\.[A-Za-z][A-Za-z0-9_-]{0,63})?|node\.[A-Za-z0-9_-]{1,64}\.output)$/;
export function workflowKnowledgeIssue(graph: WorkflowGraph): string {
  const positions = new Map(topologicalOrder(graph).map((id, index) => [id, index]));
  for (const node of graph.nodes) {
    if (node.type !== 'knowledge_search') continue;
    const query = node.config.query;
    if (query !== undefined && (typeof query !== 'string' || query.length > 2000)) return `不能保存：节点“${node.label || node.id}”的检索问题不能超过 2000 个字符。`;
    const text = typeof query === 'string' ? query : '';
    const matches = [...text.matchAll(workflowKnowledgeTemplate)].map(match => match[1]);
    if ((text.includes('{{') || text.includes('}}')) && matches.length !== (text.match(/\{\{/g) || []).length) return `不能保存：节点“${node.label || node.id}”只能引用 input 或上游 node 输出。`;
    for (const source of matches) {
      if (!workflowKnowledgeSource.test(source) || !source.startsWith('node.')) continue;
      const sourceId = source.slice(5, -7);
      if (!positions.has(sourceId)) return `不能保存：节点“${node.label || node.id}”引用的来源节点不存在。`;
      if (sourceId === node.id || (positions.get(sourceId) ?? 0) >= (positions.get(node.id) ?? 0)) return `不能保存：节点“${node.label || node.id}”只能引用已经完成的上游节点。`;
    }
    const topK = node.config.top_k === undefined ? 6 : node.config.top_k;
    if (typeof topK !== 'number' || !Number.isInteger(topK) || topK < 1 || topK > 20) return `不能保存：节点“${node.label || node.id}”的召回条数必须在 1 到 20 之间。`;
    const mode = node.config.retrieval_mode === undefined ? 'hybrid' : node.config.retrieval_mode;
    if (mode !== 'hybrid' && mode !== 'bm25' && mode !== 'vector') return `不能保存：节点“${node.label || node.id}”的检索模式无效。`;
    const contextChars = node.config.context_chars === undefined ? 12000 : node.config.context_chars;
    if (typeof contextChars !== 'number' || !Number.isInteger(contextChars) || contextChars < 0 || contextChars > 12000) return `不能保存：节点“${node.label || node.id}”的上下文长度必须在 0 到 12000 之间。`;
  }
  return '';
}
export function workflowConditionIssue(graph: WorkflowGraph): string {
  const operators = new Set(['exists', 'eq', 'neq', 'gt', 'gte', 'lt', 'lte', 'contains', 'in']);
  for (const node of graph.nodes) {
    if (node.type !== 'condition') continue;
    const operator = node.config.operator === undefined ? 'exists' : node.config.operator;
    if (typeof operator !== 'string' || !operators.has(operator)) return `不能保存：节点“${node.label || node.id}”的条件运算符无效。`;
    const outgoing = graph.edges.filter(edge => edge.source === node.id);
    if (outgoing.length > 1 && outgoing.every(edge => !edge.label?.trim())) return `不能保存：节点“${node.label || node.id}”有多个出口，请填写连线标签。`;
  }
  return '';
}

export default function WorkflowPage({ assistantId, editable, resetEpoch = 0, onDirtyChange, onSavingChange }: { assistantId: string; editable: boolean; resetEpoch?: number; onDirtyChange?: (dirty: boolean) => void; onSavingChange?: (saving: boolean) => void }) {
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [templates, setTemplates] = useState<WorkflowTemplate[]>([]);
  const [assistant, setAssistant] = useState<Assistant | null>(null);
  const [models, setModels] = useState<ModelOption[]>([]);
  const [selected, setSelected] = useState<Workflow | null>(null);
  const [graph, setGraph] = useState<WorkflowGraph>(cloneGraph(DEFAULT_WORKFLOW_GRAPH));
  const [selectedNode, setSelectedNode] = useState('agent');
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [newName, setNewName] = useState('');
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [run, setRun] = useState<WorkflowRun | null>(null);
  const [approvalComment, setApprovalComment] = useState('');
  const [runInput, setRunInput] = useState<Record<string, unknown>>({});
  const [edgeSource, setEdgeSource] = useState('');
  const [edgeTarget, setEdgeTarget] = useState('');
  const [canvasResetToken, setCanvasResetToken] = useState(0);
  const dirtyCallbackRef = useRef(onDirtyChange);
  const savingCallbackRef = useRef(onSavingChange);
  dirtyCallbackRef.current = onDirtyChange;
  savingCallbackRef.current = onSavingChange;

  const replaceGraph = (next: WorkflowGraph | ((current: WorkflowGraph) => WorkflowGraph)) => {
    setCanvasResetToken(value => value + 1);
    setGraph(next);
  };

  const handleCanvasGraphChange = useCallback((next: WorkflowGraph) => {
    setGraph(current => JSON.stringify(current) === JSON.stringify(next) ? current : next);
  }, []);

  const load = async (workflowId?: string) => {
    setLoading(true); setError('');
    try {
      const [values, availableTemplates, assistantValue, availableModels] = await Promise.all([
        api<Workflow[]>(`/assistants/${encodeURIComponent(assistantId)}/workflows`),
        api<WorkflowTemplate[]>('/workflow-templates'),
        api<Assistant>(`/assistants/${encodeURIComponent(assistantId)}`),
        api<ModelOption[]>('/models'),
      ]);
      setWorkflows(values);
      setTemplates(availableTemplates);
      setAssistant(assistantValue);
      setModels(availableModels);
      const id = workflowId || selected?.id || values[0]?.id;
      if (id) await selectWorkflow(id, values);
      else { setSelected(null); replaceGraph(cloneGraph(DEFAULT_WORKFLOW_GRAPH)); }
    } catch (cause) { setError(errorText(cause)); }
    finally { setLoading(false); }
  };

  const selectWorkflow = async (workflowId: string, list = workflows) => {
    setError('');
    try {
      const detail = await api<Workflow>(`/workflows/${encodeURIComponent(workflowId)}`);
      setSelected(detail); setWorkflows(list.map(item => item.id === detail.id ? { ...item, ...detail } : item));
      setName(detail.name); setDescription(detail.description); replaceGraph(graphFrom(detail));
      const nextGraph = graphFrom(detail); const first = nextGraph.nodes[0]?.id || ''; setSelectedNode(first); setEdgeSource(first); setEdgeTarget(nextGraph.nodes[1]?.id || ''); setRunInput(Object.fromEntries(workflowFormFields(nextGraph).map(field => [field.id, field.type === 'number' ? '' : '']))); setRun(null);
      setApprovalComment('');
    } catch (cause) { setError(errorText(cause)); }
  };

  useEffect(() => { void load(); }, [assistantId]); // eslint-disable-line react-hooks/exhaustive-deps

  const activeNode = graph.nodes.find(node => node.id === selectedNode) || graph.nodes[0];
  const activeNodeFields = activeNode ? workflowFormFields({ nodes: [activeNode], edges: [] }) : [];
  const activeModelId = nodeConfigText(activeNode, 'model_id', assistant?.default_model_id || '');
  const activeModel = models.find(model => model.id === activeModelId);
  const hasModelNodes = graph.nodes.some(node => node.type === 'agent' && node.config.mode === 'model');
  const runIsDebug = run?.output?.debug === true;
  const runDebugNodeId = typeof run?.output?.node_id === 'string' ? run.output.node_id : '';
  const pendingApproval = run?.approvals?.find(item => item.status === 'pending') || (run?.approval?.status === 'pending' ? run.approval : null);
  const runStatusLabel = run?.status === 'awaiting_approval' ? '等待人工审批' : run?.status === 'failed' ? '运行失败' : run?.synthetic ? '合成运行成功' : '模型运行成功';
  const updateActiveNodeConfig = (changes: Record<string, unknown>, refreshCanvas = false) => {
    if (!activeNode) return;
    const update = (value: WorkflowGraph) => ({ ...value, nodes: value.nodes.map(node => node.id === activeNode.id ? { ...node, config: { ...node.config, ...changes } } : node) });
    if (refreshCanvas) replaceGraph(update);
    else setGraph(update);
  };
  const dirty = !!selected && (name !== selected.name || description !== selected.description || JSON.stringify(graph) !== JSON.stringify(graphFrom(selected)));
  const graphIssue = graphHasCycle(graph)
    ? '不能保存：工作流不能形成循环依赖。'
    : graphHasIsolatedNode(graph)
      ? '不能保存：每个节点都必须连接到工作流。'
      : workflowInputBindingsIssue(graph) || workflowKnowledgeIssue(graph) || workflowConditionIssue(graph);

  useEffect(() => {
    dirtyCallbackRef.current?.(dirty);
  }, [dirty]);

  useEffect(() => () => dirtyCallbackRef.current?.(false), []);

  useEffect(() => {
    savingCallbackRef.current?.(busy);
  }, [busy]);

  useEffect(() => () => savingCallbackRef.current?.(false), []);

  const create = async () => {
    if (!editable || !newName.trim()) return;
    setBusy(true); setError('');
    try {
      const created = await post<Workflow>(`/assistants/${encodeURIComponent(assistantId)}/workflows`, { name: newName.trim(), description: '本地合成流程，可按业务继续配置。', graph: DEFAULT_WORKFLOW_GRAPH });
      setNewName(''); setNotice('工作流草稿已创建。'); await load(created.id);
    } catch (cause) { setError(errorText(cause)); }
    finally { setBusy(false); }
  };

  const createFromTemplate = async (template: WorkflowTemplate) => {
    if (!editable) return;
    setBusy(true); setError('');
    try {
      const created = await post<Workflow & { template_id: string }>(`/assistants/${encodeURIComponent(assistantId)}/workflows/from-template`, { template_id: template.template_id });
      setNotice(`${template.name} 已复制为新的合成草稿。`); await load(created.id);
    } catch (cause) { setError(errorText(cause)); }
    finally { setBusy(false); }
  };

  const save = async () => {
    if (!selected || !editable || !dirty || graphIssue) return;
    setBusy(true); setError('');
    try { const saved = await patch<Workflow>(`/workflows/${encodeURIComponent(selected.id)}/draft`, { expected_revision: selected.revision, name, description, graph }); setSelected(saved); setWorkflows(values => values.map(item => item.id === saved.id ? saved : item)); replaceGraph(graphFrom(saved)); setNotice(`草稿 v${saved.draft_version} 已保存。`); }
    catch (cause) { setError(errorText(cause)); }
    finally { setBusy(false); }
  };

  const publish = async () => {
    if (!selected || !editable) return;
    if (dirty) { setError('请先保存当前草稿，再发布。'); return; }
    setBusy(true); setError('');
    try { const published = await post<Workflow>(`/workflows/${encodeURIComponent(selected.id)}/publish`); setSelected(published); setWorkflows(values => values.map(item => item.id === published.id ? published : item)); setNotice(`v${published.published_version} 已发布，运行会固定此版本。`); }
    catch (cause) { setError(errorText(cause)); }
    finally { setBusy(false); }
  };

  const rollback = async (version: number) => {
    if (!selected || !editable) return;
    setBusy(true); setError('');
    try { const rolled = await post<Workflow>(`/workflows/${encodeURIComponent(selected.id)}/rollback`, { version }); setSelected(rolled); setWorkflows(values => values.map(item => item.id === rolled.id ? rolled : item)); replaceGraph(graphFrom(rolled)); setName(rolled.name); setDescription(rolled.description); setNotice(`已从 v${version} 建立新的未发布草稿。`); }
    catch (cause) { setError(errorText(cause)); }
    finally { setBusy(false); }
  };

  const testRun = async () => {
    if (!selected || selected.published_version === null || dirty) { setError(dirty ? '请先保存草稿；测试只运行已发布版本。' : '请先发布一个版本，再运行合成测试。'); return; }
    setBusy(true); setError('');
    try {
      const next = await post<WorkflowRun>(`/workflows/${encodeURIComponent(selected.id)}/runs`, { input: runInput });
      setRun(next);
      setApprovalComment('');
      setNotice(next.status === 'awaiting_approval' ? '工作流已暂停，等待审批节点的明确决定。' : hasModelNodes ? '工作流运行完成，智能体节点已使用发布版本中的模型配置。' : '合成测试完成，未调用外部模型或业务工具。');
    }
    catch (cause) { setError(errorText(cause)); }
    finally { setBusy(false); }
  };

  const decideApproval = async (approval: WorkflowApproval, decision: 'approve' | 'reject') => {
    if (!run || approval.status !== 'pending') return;
    setBusy(true); setError('');
    try {
      const next = await post<WorkflowRun>(`/workflow-runs/${encodeURIComponent(run.id)}/approvals/${encodeURIComponent(approval.node_id)}/decision`, { decision, comment: approvalComment.trim() });
      setRun(next);
      setApprovalComment('');
      setNotice(decision === 'approve' ? (next.status === 'awaiting_approval' ? '审批已通过，工作流已进入下一个审批点。' : '审批已通过，工作流继续执行并完成。') : '审批已拒绝，工作流已停止并保留审计记录。');
    } catch (cause) { setError(errorText(cause)); }
    finally { setBusy(false); }
  };

  const debugNode = async () => {
    if (!selected || !activeNode || activeNode.type !== 'agent' || nodeConfigText(activeNode, 'mode', 'synthetic') !== 'model' || !editable) return;
    if (dirty) { setError('请先保存当前草稿，再调试节点。'); return; }
    setBusy(true); setError('');
    try { setRun(await post<WorkflowRun>(`/workflows/${encodeURIComponent(selected.id)}/nodes/${encodeURIComponent(activeNode.id)}/debug`, { input: runInput })); setNotice(`节点“${activeNode.label}”调试完成，使用的是当前草稿配置。`); }
    catch (cause) { setError(errorText(cause)); }
    finally { setBusy(false); }
  };

  const addNode = (type: WorkflowNodeType) => {
    if (!editable) return;
    const id = nodeId(type, graph.nodes); const node: WorkflowNode = { id, type, label: NODE_META[type].label, config: type === 'agent' || type === 'knowledge_search' || type === 'mcp_read' ? { mode: 'synthetic' } : {} };
    const previous = graph.nodes[graph.nodes.length - 1];
    replaceGraph(value => ({ nodes: [...value.nodes, node], edges: previous ? [...value.edges, { source: previous.id, target: id, label: '下一步' }] : value.edges })); setSelectedNode(id); setEdgeSource(previous?.id || id); setEdgeTarget(id);
  };

  const removeNode = () => { if (!editable || !activeNode || graph.nodes.length <= 1) return; replaceGraph(value => ({ nodes: value.nodes.filter(node => node.id !== activeNode.id), edges: value.edges.filter(edge => edge.source !== activeNode.id && edge.target !== activeNode.id) })); setSelectedNode(graph.nodes.find(node => node.id !== activeNode.id)?.id || ''); };
  const addEdge = () => { if (!editable || !edgeSource || !edgeTarget || edgeSource === edgeTarget || graph.edges.some(edge => edge.source === edgeSource && edge.target === edgeTarget)) return; const next = { ...graph, edges: [...graph.edges, { source: edgeSource, target: edgeTarget, label: '下一步' }] }; if (graphHasCycle(next)) { setError('不能添加这条连线：工作流不能形成循环。'); return; } replaceGraph(next); };

  if (loading) return <div className="workflow-page"><Loader text="正在读取工作流…"/></div>;
  return <div className="workflow-page">
    <PageHeader eyebrow="低代码编排" title="工作流" description="像搭建 Coze 一样组合智能体、知识检索、审批和产物；每个智能体都可以绑定助手模型并写清角色与系统提示词。" actions={editable ? <div className="workflow-create"><input aria-label="新工作流名称" value={newName} placeholder="新工作流名称" maxLength={120} onChange={event => setNewName(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') void create(); }}/><Button busy={busy} disabled={!newName.trim()} onClick={()=>void create()}><Plus size={15}/>创建工作流</Button></div> : undefined}/>
    {error && <ErrorNotice message={error}/>}<SuccessNotice message={notice}/>
    <div className="workflow-layout">
      <aside className="workflow-library" aria-label="工作流列表"><div className="workflow-library-head"><div><span className="eyebrow">能力池</span><strong>{workflows.length} 个工作流</strong></div><GitBranch size={20}/></div>{workflows.length ? workflows.map(item => <button type="button" key={item.id} className={`workflow-list-item ${selected?.id === item.id ? 'selected' : ''}`} onClick={()=>void selectWorkflow(item.id)}><span className="workflow-list-dot"/><span><strong>{item.name}</strong><small>{item.published_version ? `已发布 v${item.published_version}` : '仅草稿'} · v{item.revision}</small></span><ChevronRight size={15}/></button>) : <div className="workflow-empty"><GitBranch size={27}/><p>还没有工作流</p><small>从一个业务流程开始，把能力组合起来。</small></div>}<div className="workflow-template-library"><div className="workflow-template-head"><span>预置模板</span><small>全部为合成数据</small></div>{templates.map(template=><button type="button" key={template.template_id} disabled={!editable||busy} onClick={()=>void createFromTemplate(template)}><span className="workflow-template-mark"><Plus size={13}/></span><span><strong>{template.name}</strong><small>{template.description}</small></span></button>)}</div></aside>
      <section className="workflow-editor" aria-label="工作流编辑器">{!selected ? <div className="workflow-empty large"><GitBranch size={38}/><h2>建立你的第一个业务流程</h2><p>工作流由人来设计，模型只能在后续生成待审核草稿。</p></div> : <>
        <div className="workflow-editor-head"><div><div className="workflow-kicker"><span className={`workflow-status ${selected.published_version ? 'published' : ''}`}>{selected.published_version ? `已发布 v${selected.published_version}` : '草稿'}</span>{dirty && <span className="workflow-dirty-chip" aria-live="polite"><span/>未保存</span>}<span>服务端版本 v{selected.revision}</span><span className={`workflow-synthetic ${hasModelNodes ? 'workflow-model-enabled' : ''}`}><CircleDot size={11}/>{hasModelNodes ? '已接入模型节点' : '合成节点'}</span></div><input className="workflow-title-input" value={name} disabled={!editable||busy} aria-label="工作流名称" onChange={event=>setName(event.target.value)}/><input className="workflow-description-input" value={description} disabled={!editable||busy} aria-label="工作流说明" placeholder="说明这个流程解决什么业务问题" onChange={event=>setDescription(event.target.value)}/></div><div className="workflow-actions"><Button variant="secondary" disabled={!editable||busy||!dirty||!!graphIssue} onClick={()=>void save()}><Save size={15}/>保存草稿</Button><Button variant="secondary" disabled={!editable||busy||dirty} onClick={()=>void publish()}><Upload size={15}/>发布版本</Button><Button disabled={busy||selected.published_version===null||dirty} onClick={()=>void testRun()}><Play size={15}/>{hasModelNodes ? '运行工作流' : '合成测试'}</Button></div></div>
        {graphIssue && <ErrorNotice message={graphIssue}/>}
        <div className={`workflow-boundary ${hasModelNodes ? 'workflow-boundary-model' : ''}`}><ShieldCheck size={15}/><span>{hasModelNodes ? '模型边界：智能体节点只调用助手已授权的模型目录；模型密钥不进入工作流图，知识、工具和审批节点仍按各自权限执行。' : '安全边界：当前节点只生成计划和状态，不访问生产数据、凭据、模型或外部业务系统。选择“模型执行”后才会调用已授权模型。'}</span></div>
        <div className="workflow-canvas-card workflow-flowgram-card"><div className="workflow-section-head"><div><h2>流程画布</h2><p>拖拽节点、连接步骤，使用画布工具缩放和撤销；双击节点名称可直接编辑。</p></div><span>{graph.nodes.length} 节点 · {graph.edges.length} 条连线</span></div><Suspense fallback={<div className="zhixing-flowgram-loading">正在加载画布…</div>}><FlowGramWorkflowCanvas graph={graph} models={models} defaultModelId={assistant?.default_model_id} editable={editable} resetToken={`${canvasResetToken}:${busy ? 'busy' : 'ready'}:${editable ? 'editable' : 'readonly'}:${resetEpoch}`} onChange={handleCanvasGraphChange} onSelectNode={setSelectedNode} /></Suspense><div className="workflow-canvas-legend"><span><i className="legend-safe"/>节点执行边界</span><span><i className="legend-approval"/>人工审批点</span><span><i className="legend-draft"/>未发布修改</span></div></div>
        <div className="workflow-lower-grid"><section className="workflow-panel workflow-palette"><div className="workflow-section-head"><div><h2>添加能力节点</h2><p>选择节点后拖入画布，再在右侧完成运行方式和模型配置。</p></div></div><div className="workflow-palette-grid">{(Object.keys(NODE_META) as WorkflowNodeType[]).map(type=><button type="button" key={type} disabled={!editable||busy} onClick={()=>addNode(type)}><span style={{ color: NODE_META[type].color }}><Plus size={14}/></span><span><strong>{NODE_META[type].label}</strong><small>{NODE_META[type].hint}</small></span></button>)}</div></section><section className="workflow-panel workflow-inspector"><div className="workflow-section-head"><div><h2>节点配置</h2><p>智能体节点可以绑定助手模型、角色和系统提示词；凭据永远不进入流程图。</p></div>{activeNode&&<Button variant="ghost" disabled={!editable||busy||graph.nodes.length<=1} onClick={removeNode}><Trash2 size={14}/>移除</Button>}</div>{activeNode ? <div className="workflow-inspector-fields"><Field label="节点名称"><input value={activeNode.label} disabled={!editable||busy} onChange={event=>setGraph(value=>({...value,nodes:value.nodes.map(node=>node.id===activeNode.id?{...node,label:event.target.value}:node)}))} onBlur={()=>setCanvasResetToken(value=>value+1)}/></Field><Field label="节点类型"><select value={activeNode.type} disabled={!editable||busy} onChange={event=>{const type=event.target.value as WorkflowNodeType;replaceGraph(value=>({...value,nodes:value.nodes.map(node=>node.id===activeNode.id?{...node,type,label:NODE_META[type].label,config:type==='agent'||type==='knowledge_search'||type==='mcp_read'?{mode:'synthetic'}:{}}:node)}));}}>{(Object.keys(NODE_META) as WorkflowNodeType[]).map(type=><option key={type} value={type}>{NODE_META[type].label}</option>)}</select></Field>{activeNode.type==='agent'&&<><Field label="执行方式" hint="选择模型执行后，发布版本会按此节点配置调用已授权模型。"><select value={nodeConfigText(activeNode,'mode','synthetic')} disabled={!editable||busy} onChange={event=>updateActiveNodeConfig({mode:event.target.value},true)}><option value="synthetic">合成执行（不调用模型）</option><option value="model">模型执行（调用已选模型）</option></select></Field><Field label="角色定位" hint="用一句话说明这个智能体在流程中的职责。"><input value={nodeConfigText(activeNode,'role')} maxLength={200} disabled={!editable||busy} placeholder="例如：财务政策审核员" onChange={event=>updateActiveNodeConfig({role:event.target.value})}/></Field><Field label="节点任务说明" hint="这是当前节点要完成的具体任务。"><textarea rows={3} maxLength={1000} value={nodeConfigText(activeNode,'prompt')} disabled={!editable||busy} placeholder="例如：提取申请中的金额、部门和风险点" onChange={event=>updateActiveNodeConfig({prompt:event.target.value})}/></Field><Field label="系统提示词" hint="定义行为边界、输出格式和核验要求；会随发布版本固定。"><textarea rows={6} maxLength={4000} value={nodeConfigText(activeNode,'system_prompt')} disabled={!editable||busy} placeholder={assistant?.instructions||'写清楚角色、步骤、事实边界和输出格式'} onChange={event=>updateActiveNodeConfig({system_prompt:event.target.value})}/></Field><Field label="输入变量映射" hint="每行一项，例如：金额 = input.amount；结论 = node.review.output。只允许引用工作流输入或已经完成的上游节点。"><textarea rows={4} maxLength={2000} value={workflowInputBindingsText(activeNode)} disabled={!editable||busy} placeholder="金额 = input.amount\n结论 = node.review.output" onChange={event=>updateActiveNodeConfig({input_bindings:parseWorkflowInputBindings(event.target.value)})}/></Field><div className="workflow-model-grid"><Field label="模型"><select value={activeModelId} disabled={!editable||busy||!models.length} onChange={event=>{const id=event.target.value;const selectedModel=models.find(model=>model.id===id);updateActiveNodeConfig({model_id:id||null,reasoning_level:selectedModel?.default_reasoning_level||null},true);}}><option value="">使用助手默认模型</option>{models.map(model=><option key={model.id} value={model.id} disabled={!model.available}>{model.label}{!model.available?'（未开通）':''}</option>)}{activeModelId&&!activeModel&&<option value={activeModelId}>当前模型（暂不可用）</option>}</select></Field><Field label="推理级别"><select value={nodeConfigText(activeNode,'reasoning_level',activeModel?.default_reasoning_level||'')} disabled={!editable||busy||!activeModel} onChange={event=>updateActiveNodeConfig({reasoning_level:event.target.value||null},true)}><option value="">模型默认</option>{activeModel?.reasoning_levels.map(level=><option key={level.id} value={level.id}>{level.label}</option>)}</select></Field></div><div className="workflow-model-advanced"><Field label="温度" hint="越低越稳定；会作为发布版本的一部分固定。"><input type="number" min={0} max={2} step={0.1} value={nodeConfigNumber(activeNode,'temperature',0.2).toFixed(1)} disabled={!editable||busy} onChange={event=>updateActiveNodeConfig({temperature:Math.max(0,Math.min(2,Number(event.target.value)||0))})}/></Field><Field label="最大输出" hint="限制单个节点的输出长度。"><input type="number" min={32} max={4096} step={32} value={nodeConfigNumber(activeNode,'max_tokens',512)} disabled={!editable||busy} onChange={event=>updateActiveNodeConfig({max_tokens:Math.max(32,Math.min(4096,Number(event.target.value)||32))})}/></Field></div>{nodeConfigText(activeNode,'mode','synthetic')==='model'&&<div className="workflow-model-status"><Check size={14}/><span><strong>{activeModel?.label||'助手默认模型'}</strong><small>{dirty?'请先保存草稿，再调试当前节点。':activeModel?.available===false?'模型当前不可用，请重新选择。':'发布后运行会调用此模型；模型密钥由服务端管理。'}</small></span><Button variant="secondary" busy={busy} disabled={!editable||busy||dirty||!activeModel||activeModel.available===false} onClick={()=>void debugNode()}><Play size={13}/>调试此节点</Button></div>}</>}{activeNode.type==='knowledge_search'&&<><Field label="检索问题" hint="支持 {{input.topic}} 或 {{node.review.output}} 模板；运行时只检索助手已授权且启用的知识库。"><textarea rows={4} maxLength={2000} value={nodeConfigText(activeNode,'query')} disabled={!editable||busy} placeholder="例如：请检索 {{input.topic}} 的质量标准" onChange={event=>updateActiveNodeConfig({query:event.target.value})}/></Field><div className="workflow-model-grid"><Field label="召回条数" hint="最多 20 条，结果会带文件版本和来源摘要。"><input type="number" min={1} max={20} step={1} value={nodeConfigNumber(activeNode,'top_k',6)} disabled={!editable||busy} onChange={event=>updateActiveNodeConfig({top_k:Math.max(1,Math.min(20,Number(event.target.value)||1))})}/></Field><Field label="检索模式"><select value={nodeConfigText(activeNode,'retrieval_mode','hybrid')} disabled={!editable||busy} onChange={event=>updateActiveNodeConfig({retrieval_mode:event.target.value})}><option value="hybrid">混合检索</option><option value="bm25">关键词检索</option><option value="vector">向量检索</option></select></Field></div><Field label="上下文长度" hint="传给后续智能体的检索上下文上限，最多 12000 字符。"><input type="number" min={0} max={12000} step={500} value={nodeConfigNumber(activeNode,'context_chars',12000)} disabled={!editable||busy} onChange={event=>updateActiveNodeConfig({context_chars:Math.max(0,Math.min(12000,Number(event.target.value)||0))})}/></Field><div className="workflow-node-contract"><Check size={14}/><span>执行模式：<strong>授权知识检索</strong><small>节点不保存知识库编号；运行时读取当前助手已授权、已启用的知识库，并保留文件版本、摘要和来源证据。</small></span></div></>}{activeNode.type==='condition'&&<><Field label="判断字段" hint="读取当前工作流输入中的英文字段，例如 amount。"><input value={nodeConfigText(activeNode,'field')} maxLength={64} disabled={!editable||busy} placeholder="例如：amount" onChange={event=>updateActiveNodeConfig({field:event.target.value})}/></Field><div className="workflow-model-grid"><Field label="运算符"><select value={nodeConfigText(activeNode,'operator','exists')} disabled={!editable||busy} onChange={event=>updateActiveNodeConfig({operator:event.target.value})}><option value="exists">存在</option><option value="eq">等于</option><option value="neq">不等于</option><option value="gt">大于</option><option value="gte">大于等于</option><option value="lt">小于</option><option value="lte">小于等于</option><option value="contains">包含</option><option value="in">属于列表</option></select></Field><Field label="比较值"><input value={nodeConfigValueText(activeNode,'value')} disabled={!editable||busy} placeholder="例如：5000" onChange={event=>updateActiveNodeConfig({value:conditionValue(event.target.value)})}/></Field></div><div className="workflow-model-grid"><Field label="满足时标签" hint="条件为真时选择同名连线。"><input value={nodeConfigText(activeNode,'true_label')} maxLength={120} disabled={!editable||busy} placeholder="例如：通过" onChange={event=>updateActiveNodeConfig({true_label:event.target.value})}/></Field><Field label="不满足时标签" hint="条件为假时选择同名连线。"><input value={nodeConfigText(activeNode,'false_label')} maxLength={120} disabled={!editable||busy} placeholder="例如：不通过" onChange={event=>updateActiveNodeConfig({false_label:event.target.value})}/></Field></div><div className="workflow-node-contract"><Check size={14}/><span>执行模式：<strong>确定性条件分支</strong><small>分支只读取已校验的工作流输入；未选中的出口会记录为跳过，不会继续执行。</small></span></div></>} {activeNode.type!=='agent'&&activeNode.type!=='knowledge_search'&&activeNode.type!=='condition'&&<div className="workflow-node-contract"><Check size={14}/><span>执行模式：<strong>受控本地节点</strong><small>知识、工具、审批和产物节点按服务端权限与审计契约执行。</small></span></div>}{activeNodeFields.length>0&&<div className="workflow-form-preview"><div className="workflow-form-preview-head"><strong>结构化表单字段</strong><small>运行输入</small></div>{activeNodeFields.map(field=><label key={field.id}><span>{field.label}{field.required&&<b>*</b>}</span><input aria-label={field.label} type={field.type==='number'?'number':field.type==='date'?'date':'text'} value={String(runInput[field.id]??'')} disabled={!editable||busy} onChange={event=>setRunInput(value=>({...value,[field.id]:field.type==='number'?(event.target.value===''?'':Number(event.target.value)):event.target.value}))}/></label>)}</div>}</div> : <p className="section-help">从画布选择一个节点开始配置。</p>}</section></div>
        <section className="workflow-panel workflow-edge-panel"><div className="workflow-section-head"><div><h2>连接步骤</h2><p>从一个节点指向另一个节点；条件分支可用连线标签选择出口，服务端会拒绝循环和孤立节点。</p></div></div><div className="workflow-edge-form"><select aria-label="连线起点" value={edgeSource} disabled={!editable||busy} onChange={event=>setEdgeSource(event.target.value)}>{graph.nodes.map(node=><option key={node.id} value={node.id}>{node.label}</option>)}</select><ChevronRight size={15}/><select aria-label="连线终点" value={edgeTarget} disabled={!editable||busy} onChange={event=>setEdgeTarget(event.target.value)}>{graph.nodes.map(node=><option key={node.id} value={node.id}>{node.label}</option>)}</select><Button variant="secondary" disabled={!editable||busy||!edgeSource||!edgeTarget||edgeSource===edgeTarget} onClick={addEdge}><Plus size={14}/>添加连线</Button></div><div className="workflow-edge-list">{graph.edges.map(edge=><span key={`${edge.source}-${edge.target}`}><span>{graph.nodes.find(node=>node.id===edge.source)?.label}<ChevronRight size={12}/>{graph.nodes.find(node=>node.id===edge.target)?.label}</span><input aria-label={`连线标签 ${edge.source} 到 ${edge.target}`} value={edge.label||''} placeholder="分支标签" disabled={!editable||busy} onChange={event=>setGraph(value=>({...value,edges:value.edges.map(item=>item.source===edge.source&&item.target===edge.target?{...item,label:event.target.value}:item)}))}/></span>)}</div></section>
        <section className="workflow-panel workflow-versions"><div className="workflow-section-head"><div><h2>版本与回滚</h2><p>已发布版本不可变；回滚只建立新草稿，必须再次人工发布。</p></div></div><div className="workflow-version-list">{selected.versions.map(version=><div key={version.version} className={`workflow-version-row ${version.version===selected.published_version?'current':''}`}><span className="workflow-version-number">v{version.version}</span><span><strong>{version.status==='published'?'已发布':version.status==='draft'?'草稿':'历史版本'}</strong><small>{version.graph_hash.slice(0,12)} · {new Date(version.created_at).toLocaleString('zh-CN')}</small></span>{editable&&version.version!==selected.draft_version&&<Button variant="ghost" disabled={busy} onClick={()=>void rollback(version.version)}><RotateCcw size={13}/>回滚为草稿</Button>}</div>)}</div></section>
        {run&&<section className={`workflow-run-card ${run.synthetic ? '' : 'workflow-run-model'} ${run.status === 'awaiting_approval' ? 'workflow-run-awaiting' : ''}`}><div><span className="workflow-run-pill">{run.status === 'awaiting_approval' ? <ShieldCheck size={13}/> : <Check size={13}/>} {runIsDebug ? '节点调试成功' : runStatusLabel}</span><strong>{runIsDebug ? `已调试草稿节点${runDebugNodeId ? ` · ${runDebugNodeId}` : ''}` : `已运行发布版本 v${run.version}`}</strong><small>{runIsDebug ? `草稿版本 v${run.version} · ` : ''}图哈希 {run.graph_hash.slice(0,16)} · 权限摘要 {run.permission_digest.slice(0,16)}</small>{pendingApproval&&<div className="workflow-run-approval"><span><strong>审批节点：{pendingApproval.node_id}</strong><small>明确选择后才会继续，决定会写入运行审计。</small></span><input aria-label="审批说明" maxLength={1000} value={approvalComment} placeholder="可选：写下审批依据" onChange={event=>setApprovalComment(event.target.value)}/><div><Button variant="secondary" disabled={busy} onClick={()=>void decideApproval(pendingApproval, 'approve')}>批准继续</Button><Button variant="ghost" disabled={busy} onClick={()=>void decideApproval(pendingApproval, 'reject')}>拒绝运行</Button></div></div>}</div><div className="workflow-run-trace">{run.trace.map(item=><span className={`workflow-run-trace-item ${item.output_preview ? 'has-output' : ''}`} key={item.node_id} title={item.output_preview || undefined}><span><CircleDot size={12}/>{item.node_id}{item.model_label&&<small>{item.model_label}</small>}<small>{item.status}</small></span>{item.output_preview&&<em>{item.output_preview}</em>}</span>)}</div></section>}
      </>}</section>
    </div>
  </div>;
}
