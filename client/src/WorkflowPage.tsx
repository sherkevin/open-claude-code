import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react';
import { Check, ChevronRight, CircleDot, GitBranch, Play, Plus, RotateCcw, Save, ShieldCheck, Trash2, Upload } from 'lucide-react';
import { api, errorText, patch, post } from './api';
import { Button, ErrorNotice, Field, Loader, PageHeader, SuccessNotice } from './components';
import type { Workflow, WorkflowGraph, WorkflowNode, WorkflowNodeType, WorkflowRun, WorkflowTemplate } from './types';
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

export default function WorkflowPage({ assistantId, editable, resetEpoch = 0, onDirtyChange, onSavingChange }: { assistantId: string; editable: boolean; resetEpoch?: number; onDirtyChange?: (dirty: boolean) => void; onSavingChange?: (saving: boolean) => void }) {
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [templates, setTemplates] = useState<WorkflowTemplate[]>([]);
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
      const values = await api<Workflow[]>(`/assistants/${encodeURIComponent(assistantId)}/workflows`);
      const availableTemplates = await api<WorkflowTemplate[]>('/workflow-templates');
      setWorkflows(values);
      setTemplates(availableTemplates);
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
    } catch (cause) { setError(errorText(cause)); }
  };

  useEffect(() => { void load(); }, [assistantId]); // eslint-disable-line react-hooks/exhaustive-deps

  const activeNode = graph.nodes.find(node => node.id === selectedNode) || graph.nodes[0];
  const activeNodeFields = activeNode ? workflowFormFields({ nodes: [activeNode], edges: [] }) : [];
  const dirty = !!selected && (name !== selected.name || description !== selected.description || JSON.stringify(graph) !== JSON.stringify(graphFrom(selected)));
  const graphIssue = graphHasCycle(graph)
    ? '不能保存：工作流不能形成循环依赖。'
    : graphHasIsolatedNode(graph)
      ? '不能保存：每个节点都必须连接到工作流。'
      : '';

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
    try { setRun(await post<WorkflowRun>(`/workflows/${encodeURIComponent(selected.id)}/runs`, { input: runInput })); setNotice('合成测试完成，未调用外部模型或业务工具。'); }
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
    <PageHeader eyebrow="低代码编排" title="工作流" description="把智能体、知识检索、审批和产物组合成可审计的业务流程。当前编辑与运行均为本地合成模式。" actions={editable ? <div className="workflow-create"><input aria-label="新工作流名称" value={newName} placeholder="新工作流名称" maxLength={120} onChange={event => setNewName(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') void create(); }}/><Button busy={busy} disabled={!newName.trim()} onClick={()=>void create()}><Plus size={15}/>创建工作流</Button></div> : undefined}/>
    {error && <ErrorNotice message={error}/>}<SuccessNotice message={notice}/>
    <div className="workflow-layout">
      <aside className="workflow-library" aria-label="工作流列表"><div className="workflow-library-head"><div><span className="eyebrow">能力池</span><strong>{workflows.length} 个工作流</strong></div><GitBranch size={20}/></div>{workflows.length ? workflows.map(item => <button type="button" key={item.id} className={`workflow-list-item ${selected?.id === item.id ? 'selected' : ''}`} onClick={()=>void selectWorkflow(item.id)}><span className="workflow-list-dot"/><span><strong>{item.name}</strong><small>{item.published_version ? `已发布 v${item.published_version}` : '仅草稿'} · v{item.revision}</small></span><ChevronRight size={15}/></button>) : <div className="workflow-empty"><GitBranch size={27}/><p>还没有工作流</p><small>从一个业务流程开始，把能力组合起来。</small></div>}<div className="workflow-template-library"><div className="workflow-template-head"><span>预置模板</span><small>全部为合成数据</small></div>{templates.map(template=><button type="button" key={template.template_id} disabled={!editable||busy} onClick={()=>void createFromTemplate(template)}><span className="workflow-template-mark"><Plus size={13}/></span><span><strong>{template.name}</strong><small>{template.description}</small></span></button>)}</div></aside>
      <section className="workflow-editor" aria-label="工作流编辑器">{!selected ? <div className="workflow-empty large"><GitBranch size={38}/><h2>建立你的第一个业务流程</h2><p>工作流由人来设计，模型只能在后续生成待审核草稿。</p></div> : <>
        <div className="workflow-editor-head"><div><div className="workflow-kicker"><span className={`workflow-status ${selected.published_version ? 'published' : ''}`}>{selected.published_version ? `已发布 v${selected.published_version}` : '草稿'}</span><span>服务端版本 v{selected.revision}</span><span className="workflow-synthetic"><CircleDot size={11}/>合成环境</span></div><input className="workflow-title-input" value={name} disabled={!editable||busy} aria-label="工作流名称" onChange={event=>setName(event.target.value)}/><input className="workflow-description-input" value={description} disabled={!editable||busy} aria-label="工作流说明" placeholder="说明这个流程解决什么业务问题" onChange={event=>setDescription(event.target.value)}/></div><div className="workflow-actions"><Button variant="secondary" disabled={!editable||busy||!dirty||!!graphIssue} onClick={()=>void save()}><Save size={15}/>保存草稿</Button><Button variant="secondary" disabled={!editable||busy||dirty} onClick={()=>void publish()}><Upload size={15}/>发布版本</Button><Button disabled={busy||selected.published_version===null||dirty} onClick={()=>void testRun()}><Play size={15}/>合成测试</Button></div></div>
        {graphIssue && <ErrorNotice message={graphIssue}/>}
        <div className="workflow-boundary"><ShieldCheck size={15}/><span>安全边界：节点只生成计划和状态，不访问生产数据、凭据、模型或外部业务系统。</span></div>
        <div className="workflow-canvas-card workflow-flowgram-card"><div className="workflow-section-head"><div><h2>流程画布</h2><p>拖拽节点、连接步骤，使用画布工具缩放和撤销；双击节点名称可直接编辑。</p></div><span>{graph.nodes.length} 节点 · {graph.edges.length} 条连线</span></div><Suspense fallback={<div className="zhixing-flowgram-loading">正在加载画布…</div>}><FlowGramWorkflowCanvas graph={graph} editable={editable} resetToken={`${canvasResetToken}:${busy ? 'busy' : 'ready'}:${editable ? 'editable' : 'readonly'}:${resetEpoch}`} onChange={handleCanvasGraphChange} onSelectNode={setSelectedNode} /></Suspense><div className="workflow-canvas-legend"><span><i className="legend-safe"/>节点执行边界</span><span><i className="legend-approval"/>人工审批点</span><span><i className="legend-draft"/>未发布修改</span></div></div>
        <div className="workflow-lower-grid"><section className="workflow-panel workflow-palette"><div className="workflow-section-head"><div><h2>添加能力节点</h2><p>只展示当前安全契约允许的节点。</p></div></div><div className="workflow-palette-grid">{(Object.keys(NODE_META) as WorkflowNodeType[]).map(type=><button type="button" key={type} disabled={!editable||busy} onClick={()=>addNode(type)}><span style={{ color: NODE_META[type].color }}><Plus size={14}/></span><span><strong>{NODE_META[type].label}</strong><small>{NODE_META[type].hint}</small></span></button>)}</div></section><section className="workflow-panel workflow-inspector"><div className="workflow-section-head"><div><h2>节点配置</h2><p>配置只保存业务标签，不保存凭据或私有提示词。</p></div>{activeNode&&<Button variant="ghost" disabled={!editable||busy||graph.nodes.length<=1} onClick={removeNode}><Trash2 size={14}/>移除</Button>}</div>{activeNode ? <div className="workflow-inspector-fields"><Field label="节点名称"><input value={activeNode.label} disabled={!editable||busy} onChange={event=>setGraph(value=>({...value,nodes:value.nodes.map(node=>node.id===activeNode.id?{...node,label:event.target.value}:node)}))} onBlur={()=>setCanvasResetToken(value=>value+1)}/></Field><Field label="节点类型"><select value={activeNode.type} disabled={!editable||busy} onChange={event=>{const type=event.target.value as WorkflowNodeType;replaceGraph(value=>({...value,nodes:value.nodes.map(node=>node.id===activeNode.id?{...node,type,label:NODE_META[type].label,config:type==='agent'||type==='knowledge_search'||type==='mcp_read'?{mode:'synthetic'}:{}}:node)}));}}>{(Object.keys(NODE_META) as WorkflowNodeType[]).map(type=><option key={type} value={type}>{NODE_META[type].label}</option>)}</select></Field><div className="workflow-node-contract"><Check size={14}/><span>执行模式：<strong>synthetic</strong><small>真实模型和外部工具需另行授权验收。</small></span></div>{activeNodeFields.length>0&&<div className="workflow-form-preview"><div className="workflow-form-preview-head"><strong>结构化表单字段</strong><small>合成模板输入</small></div>{activeNodeFields.map(field=><label key={field.id}><span>{field.label}{field.required&&<b>*</b>}</span><input aria-label={field.label} type={field.type==='number'?'number':field.type==='date'?'date':'text'} value={String(runInput[field.id]??'')} disabled={!editable||busy} onChange={event=>setRunInput(value=>({...value,[field.id]:field.type==='number'?(event.target.value===''?'':Number(event.target.value)):event.target.value}))}/></label>)}</div>}</div> : <p className="section-help">从画布选择一个节点开始配置。</p>}</section></div>
        <section className="workflow-panel workflow-edge-panel"><div className="workflow-section-head"><div><h2>连接步骤</h2><p>从一个节点指向另一个节点，服务端会拒绝循环和孤立节点。</p></div></div><div className="workflow-edge-form"><select aria-label="连线起点" value={edgeSource} disabled={!editable||busy} onChange={event=>setEdgeSource(event.target.value)}>{graph.nodes.map(node=><option key={node.id} value={node.id}>{node.label}</option>)}</select><ChevronRight size={15}/><select aria-label="连线终点" value={edgeTarget} disabled={!editable||busy} onChange={event=>setEdgeTarget(event.target.value)}>{graph.nodes.map(node=><option key={node.id} value={node.id}>{node.label}</option>)}</select><Button variant="secondary" disabled={!editable||busy||!edgeSource||!edgeTarget||edgeSource===edgeTarget} onClick={addEdge}><Plus size={14}/>添加连线</Button></div><div className="workflow-edge-list">{graph.edges.map(edge=><span key={`${edge.source}-${edge.target}`}>{graph.nodes.find(node=>node.id===edge.source)?.label}<ChevronRight size={12}/>{graph.nodes.find(node=>node.id===edge.target)?.label}</span>)}</div></section>
        <section className="workflow-panel workflow-versions"><div className="workflow-section-head"><div><h2>版本与回滚</h2><p>已发布版本不可变；回滚只建立新草稿，必须再次人工发布。</p></div></div><div className="workflow-version-list">{selected.versions.map(version=><div key={version.version} className={`workflow-version-row ${version.version===selected.published_version?'current':''}`}><span className="workflow-version-number">v{version.version}</span><span><strong>{version.status==='published'?'已发布':version.status==='draft'?'草稿':'历史版本'}</strong><small>{version.graph_hash.slice(0,12)} · {new Date(version.created_at).toLocaleString('zh-CN')}</small></span>{editable&&version.version!==selected.draft_version&&<Button variant="ghost" disabled={busy} onClick={()=>void rollback(version.version)}><RotateCcw size={13}/>回滚为草稿</Button>}</div>)}</div></section>
        {run&&<section className="workflow-run-card"><div><span className="workflow-run-pill"><Check size={13}/>合成运行成功</span><strong>已运行发布版本 v{run.version}</strong><small>图哈希 {run.graph_hash.slice(0,16)} · 权限摘要 {run.permission_digest.slice(0,16)}</small></div><div className="workflow-run-trace">{run.trace.map(item=><span key={item.node_id}><CircleDot size={12}/>{item.node_id}<small>{item.status}</small></span>)}</div></section>}
      </>}</section>
    </div>
  </div>;
}
