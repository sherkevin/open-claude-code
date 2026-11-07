import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react';
import { Check, ChevronRight, CircleDot, Copy, GitBranch, History, Play, Plus, RotateCcw, Save, ShieldCheck, Trash2, Upload } from 'lucide-react';
import { api, errorText, patch, post } from './api';
import { Button, ErrorNotice, Field, Loader, PageHeader, SuccessNotice } from './components';
import type { Assistant, Connection, Conversation, ModelOption, ProviderPreflight, Workflow, WorkflowApproval, WorkflowGraph, WorkflowNode, WorkflowNodeType, WorkflowRun, WorkflowRunSummary, WorkflowTemplate } from './types';
import { workflowGraphHasCycle } from './workflowFlowgram';

const FlowGramWorkflowCanvas = lazy(() => import('./FlowGramWorkflowCanvas'));

export const DEFAULT_WORKFLOW_GRAPH: WorkflowGraph = {
  nodes: [
    { id: 'start', type: 'input', label: '开始 / 输入', config: { mode: 'synthetic', form_fields: [] } },
    { id: 'agent', type: 'agent', label: '拆解业务目标', config: { mode: 'synthetic' } },
    { id: 'knowledge', type: 'knowledge_search', label: '检索公司知识', config: { mode: 'synthetic' } },
    { id: 'approval', type: 'approval', label: '人工确认', config: {} },
  ],
  edges: [
    { source: 'start', target: 'agent', label: '提交输入' },
    { source: 'agent', target: 'knowledge', label: '需要依据' },
    { source: 'knowledge', target: 'approval', label: '提交审批' },
  ],
};

const NODE_META: Record<WorkflowNodeType, { label: string; hint: string; color: string }> = {
  input: { label: '开始 / 输入', hint: '定义每次运行的结构化输入', color: '#6e8a68' },
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
function WorkflowFormControl({ field, value, disabled, onChange }: { field: WorkflowFormField; value: unknown; disabled: boolean; onChange: (id: string, value: unknown) => void }) {
  const update = (next: string) => onChange(field.id, field.type === 'number' ? (next === '' ? '' : Number(next)) : next);
  return <label className="workflow-form-control"><span>{field.label}{field.required && <b>*</b>}</span>{field.type === 'choice' ? <select aria-label={field.label} value={String(value ?? '')} disabled={disabled} onChange={event => update(event.target.value)}><option value="">请选择</option>{(field.options || []).map(option => <option key={option} value={option}>{option}</option>)}</select> : <input aria-label={field.label} type={field.type === 'number' ? 'number' : field.type === 'date' ? 'date' : 'text'} value={String(value ?? '')} disabled={disabled} onChange={event => update(event.target.value)} />}</label>;
}
export function workflowFormFields(graph: WorkflowGraph): WorkflowFormField[] {
  const seen = new Set<string>(); const result: WorkflowFormField[] = [];
  graph.nodes.forEach(node => {
    const fields = node.config.form_fields;
    if (!Array.isArray(fields)) return;
    fields.forEach(value => { if (!value || typeof value !== 'object') return; const field = value as Record<string, unknown>; const id = typeof field.id === 'string' ? field.id : ''; if (!id || seen.has(id)) return; const type = field.type === 'number' || field.type === 'date' || field.type === 'choice' ? field.type : 'text'; result.push({ id, type, label: typeof field.label === 'string' ? field.label : id, required: field.required === true, options: Array.isArray(field.options) ? field.options.filter(item => typeof item === 'string') as string[] : undefined }); seen.add(id); });
  });
  return result;
}

export function workflowFormIssue(graph: WorkflowGraph): string {
  const idPattern = /^[A-Za-z][A-Za-z0-9_-]{0,63}$/;
  const types = new Set(['text', 'number', 'date', 'choice']);
  for (const node of graph.nodes) {
    const raw = node.config.form_fields_json ?? node.config.form_fields;
    if (raw === undefined || raw === '') continue;
    let fields: unknown = raw;
    if (typeof raw === 'string') {
      try { fields = JSON.parse(raw); } catch { return `不能保存：节点“${node.label || node.id}”的输入字段不是有效 JSON。`; }
    }
    if (!Array.isArray(fields) || fields.length > 30) return `不能保存：节点“${node.label || node.id}”的输入字段最多 30 项。`;
    const seen = new Set<string>();
    for (const field of fields) {
      if (!field || typeof field !== 'object' || Array.isArray(field)) return `不能保存：节点“${node.label || node.id}”的输入字段格式无效。`;
      const item = field as Record<string, unknown>;
      if (typeof item.id !== 'string' || !idPattern.test(item.id) || seen.has(item.id)) return `不能保存：节点“${node.label || node.id}”的输入字段标识必须唯一且使用英文标识。`;
      if (typeof item.type !== 'string' || !types.has(item.type)) return `不能保存：节点“${node.label || node.id}”的输入字段类型无效。`;
      if (typeof item.label !== 'string' || !item.label.trim() || item.label.length > 120) return `不能保存：节点“${node.label || node.id}”的输入字段名称无效。`;
      if (item.type === 'choice' && (!Array.isArray(item.options) || item.options.length === 0 || item.options.length > 30 || item.options.some(option => typeof option !== 'string'))) return `不能保存：节点“${node.label || node.id}”的选择字段必须配置 1 到 30 个文本选项。`;
      seen.add(item.id);
    }
  }
  return '';
}

export function workflowArtifactIssue(graph: WorkflowGraph): string {
  for (const node of graph.nodes) {
    if (node.type !== 'artifact') continue;
    const columns = node.config.columns;
    if (columns !== undefined && (!Array.isArray(columns) || columns.length > 50 || columns.some(item => typeof item !== 'string' || !item.trim() || item.length > 120))) return `不能保存：节点“${node.label || node.id}”的产物字段最多 50 个，且必须是非空文本。`;
    const format = node.config.format;
    if (format !== undefined && (typeof format !== 'string' || format.length > 30)) return `不能保存：节点“${node.label || node.id}”的产物格式无效。`;
  }
  return '';
}

function cloneGraph(graph: WorkflowGraph): WorkflowGraph { return { nodes: graph.nodes.map(node => ({ ...node, config: { ...node.config } })), edges: graph.edges.map(edge => ({ ...edge })) }; }
function graphFrom(workflow: Workflow): WorkflowGraph {
  const graph = cloneGraph(workflow.versions.find(version => version.version === workflow.draft_version)?.graph || DEFAULT_WORKFLOW_GRAPH);
  const order = topologicalOrder(graph);
  const positions = new Map(order.map((id, index) => [id, index]));
  return { ...graph, nodes: [...graph.nodes].sort((left, right) => (positions.get(left.id) ?? Number.MAX_SAFE_INTEGER) - (positions.get(right.id) ?? Number.MAX_SAFE_INTEGER)) };
}
function nodeId(type: WorkflowNodeType, nodes: WorkflowNode[]) { const stem = type.replace('_', '-'); let index = 1; while (nodes.some(node => node.id === `${stem}-${index}`)) index += 1; return `${stem}-${index}`; }
export function duplicateWorkflowNode(node: WorkflowNode, nodes: WorkflowNode[]): WorkflowNode {
  const config = { ...node.config };
  const ui = config.__ui;
  const position = ui && typeof ui === 'object' && !Array.isArray(ui) && (ui as Record<string, unknown>).position;
  if (position && typeof position === 'object' && !Array.isArray(position) && typeof (position as Record<string, unknown>).x === 'number' && typeof (position as Record<string, unknown>).y === 'number') {
    config.__ui = { ...(ui as Record<string, unknown>), position: { x: Number((position as Record<string, unknown>).x) + 48, y: Number((position as Record<string, unknown>).y) + 48 } };
  }
  return { ...node, id: nodeId(node.type, nodes), label: `${node.label || NODE_META[node.type].label} 副本`, config };
}
export function insertWorkflowNode(graph: WorkflowGraph, node: WorkflowNode, selectedNodeId: string): WorkflowGraph {
  const previous = graph.nodes.find(item => item.id === selectedNodeId) || graph.nodes[graph.nodes.length - 1];
  const nodes = [...graph.nodes, node];
  if (!previous) return { nodes, edges: [...graph.edges] };
  const outgoing = graph.edges.filter(edge => edge.source === previous.id);
  if (outgoing.length !== 1) return { nodes, edges: [...graph.edges, { source: previous.id, target: node.id, label: '下一步' }] };
  const edge = outgoing[0];
  return { nodes, edges: [...graph.edges.filter(item => item.source !== edge.source || item.target !== edge.target), { source: previous.id, target: node.id, label: '下一步' }, { source: node.id, target: edge.target, label: edge.label || '下一步' }] };
}
export function workflowRunHistoryLabel(run: Pick<WorkflowRunSummary, 'status' | 'synthetic' | 'execution_mode'>): string {
  if (run.status === 'awaiting_approval') return '等待审批';
  if (run.status === 'failed') return '运行失败';
  if (run.status === 'cancelled') return '已停止';
  if (run.execution_mode === 'tool') return '实时工具';
  if (run.synthetic) return '合成运行';
  return '模型运行';
}
export function workflowRunSummary(run: WorkflowRun): WorkflowRunSummary {
  return { id: run.id, workflow_id: run.workflow_id, version: run.version, status: run.status, synthetic: run.synthetic, execution_mode: run.execution_mode, trace_count: run.trace.length, approval_node_id: run.approval?.node_id || null, error: run.error || null, created_at: run.created_at, completed_at: run.completed_at };
}
function nodeConfigText(node: WorkflowNode | undefined, key: string, fallback = '') { const value = node?.config?.[key]; return typeof value === 'string' ? value : fallback; }
function nodeConfigNumber(node: WorkflowNode | undefined, key: string, fallback: number) { const value = node?.config?.[key]; return typeof value === 'number' && Number.isFinite(value) ? value : fallback; }
function nodeConfigValueText(node: WorkflowNode | undefined, key: string, fallback = '') { const value = node?.config?.[key]; return typeof value === 'string' || typeof value === 'number' ? String(value) : fallback; }
function nodeToolArgumentsText(node: WorkflowNode | undefined) { const value = node?.config?.arguments_json; return typeof value === 'string' ? value : JSON.stringify(node?.config?.arguments && typeof node.config.arguments === 'object' ? node.config.arguments : {}, null, 2); }
function nodeFormFieldsText(node: WorkflowNode | undefined) { const value = node?.config?.form_fields_json; if (typeof value === 'string') return value; return Array.isArray(node?.config?.form_fields) ? JSON.stringify(node?.config?.form_fields, null, 2) : '[]'; }
function nodeArtifactColumnsText(node: WorkflowNode | undefined) { const value = node?.config?.columns; return Array.isArray(value) ? value.filter(item => typeof item === 'string').join(', ') : ''; }
function parseArtifactColumnsText(value: string) { return value.split(',').map(item => item.trim()).filter(Boolean).slice(0, 50); }
export function nodeOutputSchemaText(node: WorkflowNode | undefined) { const value = node?.config?.output_schema_json ?? node?.config?.output_schema; if (typeof value === 'string') return value; return value && typeof value === 'object' ? JSON.stringify(value, null, 2) : ''; }
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
  const sourcePattern = /^(?:input(?:\.[A-Za-z][A-Za-z0-9_-]{0,63})?|node\.[A-Za-z0-9_-]{1,64}\.output(?:\.[A-Za-z][A-Za-z0-9_-]{0,63})?)$/;
  const nodeOutputPattern = /^node\.([A-Za-z0-9_-]{1,64})\.output(?:\.([A-Za-z][A-Za-z0-9_-]{0,63}))?$/;
  const positions = new Map(topologicalOrder(graph).map((id, index) => [id, index]));
  for (const node of graph.nodes) {
    if (node.type !== 'agent') continue;
    const seen = new Set<string>();
    for (const binding of workflowInputBindings(node)) {
      if (!keyPattern.test(binding.key) || seen.has(binding.key)) return `不能保存：节点“${node.label || node.id}”的变量名称必须唯一且使用英文标识。`;
      if (!sourcePattern.test(binding.source)) return `不能保存：节点“${node.label || node.id}”的变量来源格式无效。`;
      seen.add(binding.key);
      if (binding.source.startsWith('node.')) {
        const match = nodeOutputPattern.exec(binding.source);
        if (!match) continue;
        const sourceId = match[1];
        if (!positions.has(sourceId)) return `不能保存：节点“${node.label || node.id}”引用的来源节点不存在。`;
        if (sourceId === node.id || (positions.get(sourceId) ?? 0) >= (positions.get(node.id) ?? 0)) return `不能保存：节点“${node.label || node.id}”只能引用已经完成的上游节点。`;
        if (match[2]) {
          const sourceNode = graph.nodes.find(item => item.id === sourceId);
          const schemaText = sourceNode?.type === 'agent' && sourceNode.config.mode === 'model' ? nodeOutputSchemaText(sourceNode).trim() : '';
          let schema: Record<string, unknown> | null = null;
          try { schema = schemaText ? JSON.parse(schemaText) as Record<string, unknown> : null; } catch { schema = null; }
          const fields = Array.isArray(schema?.fields) ? schema.fields : [];
          const declared = fields.some(item => !!item && typeof item === 'object' && (item as Record<string, unknown>).name === match[2]);
          if (!schema || (!declared && schema.allow_extra !== true)) return `不能保存：节点“${node.label || node.id}”引用的来源必须是已接入模型的智能体输出字段。`;
        }
      }
    }
  }
  return '';
}
export function workflowOutputSchemaIssue(graph: WorkflowGraph): string {
  const namePattern = /^[A-Za-z][A-Za-z0-9_-]{0,63}$/;
  const types = new Set(['text', 'number', 'boolean', 'object', 'array']);
  for (const node of graph.nodes) {
    if (node.type !== 'agent') continue;
    const text = nodeOutputSchemaText(node).trim();
    if (!text) continue;
    let schema: unknown;
    try { schema = JSON.parse(text); } catch { return `不能保存：节点“${node.label || node.id}”的输出结构不是有效 JSON。`; }
    if (!schema || typeof schema !== 'object' || Array.isArray(schema)) return `不能保存：节点“${node.label || node.id}”的输出结构必须是 JSON 对象。`;
    const value = schema as Record<string, unknown>;
    if (value.allow_extra !== undefined && typeof value.allow_extra !== 'boolean') return `不能保存：节点“${node.label || node.id}”的 allow_extra 必须是布尔值。`;
    if (value.fields !== undefined && (!Array.isArray(value.fields) || value.fields.length > 20)) return `不能保存：节点“${node.label || node.id}”的输出字段最多 20 项。`;
    const seen = new Set<string>();
    for (const field of (Array.isArray(value.fields) ? value.fields : [])) {
      if (!field || typeof field !== 'object' || Array.isArray(field)) return `不能保存：节点“${node.label || node.id}”的输出字段格式无效。`;
      const item = field as Record<string, unknown>;
      if (typeof item.name !== 'string' || !namePattern.test(item.name) || seen.has(item.name)) return `不能保存：节点“${node.label || node.id}”的输出字段名称必须唯一且使用英文标识。`;
      if (typeof item.type !== 'string' || !types.has(item.type)) return `不能保存：节点“${node.label || node.id}”的输出字段类型无效。`;
      if (item.required !== undefined && typeof item.required !== 'boolean') return `不能保存：节点“${node.label || node.id}”的 required 必须是布尔值。`;
      if (item.description !== undefined && (typeof item.description !== 'string' || item.description.length > 240)) return `不能保存：节点“${node.label || node.id}”的输出字段说明过长。`;
      seen.add(item.name);
    }
  }
  return '';
}
const workflowKnowledgeTemplate = /\{\{\s*(input(?:\.[A-Za-z][A-Za-z0-9_-]{0,63})?|node\.[A-Za-z0-9_-]{1,64}\.output(?:\.[A-Za-z][A-Za-z0-9_-]{0,63})?)\s*\}\}/g;
const workflowKnowledgeSource = /^(?:input(?:\.[A-Za-z][A-Za-z0-9_-]{0,63})?|node\.[A-Za-z0-9_-]{1,64}\.output(?:\.[A-Za-z][A-Za-z0-9_-]{0,63})?)$/;
const workflowKnowledgeNodeSource = /^node\.([A-Za-z0-9_-]{1,64})\.output(?:\.([A-Za-z][A-Za-z0-9_-]{0,63}))?$/;
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
      const match = workflowKnowledgeNodeSource.exec(source);
      if (!match) return `不能保存：节点“${node.label || node.id}”的检索来源格式无效。`;
      const sourceId = match[1];
      if (!positions.has(sourceId)) return `不能保存：节点“${node.label || node.id}”引用的来源节点不存在。`;
      if (sourceId === node.id || (positions.get(sourceId) ?? 0) >= (positions.get(node.id) ?? 0)) return `不能保存：节点“${node.label || node.id}”只能引用已经完成的上游节点。`;
      if (match[2]) {
        const sourceNode = graph.nodes.find(item => item.id === sourceId);
        const schemaText = sourceNode?.type === 'agent' && sourceNode.config.mode === 'model' ? nodeOutputSchemaText(sourceNode).trim() : '';
        let schema: Record<string, unknown> | null = null;
        try { schema = schemaText ? JSON.parse(schemaText) as Record<string, unknown> : null; } catch { schema = null; }
        const fields = Array.isArray(schema?.fields) ? schema.fields : [];
        const declared = fields.some(item => !!item && typeof item === 'object' && (item as Record<string, unknown>).name === match[2]);
        if (!schema || (!declared && schema.allow_extra !== true)) return `不能保存：节点“${node.label || node.id}”引用的检索字段未在来源节点中声明。`;
      }
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
export function workflowToolArgumentsIssue(graph: WorkflowGraph): string {
  const sourcePattern = /^(?:input(?:\.[A-Za-z][A-Za-z0-9_-]{0,63})?|node\.[A-Za-z0-9_-]{1,64}\.output(?:\.[A-Za-z][A-Za-z0-9_-]{0,63})?)$/;
  const nodeOutputPattern = /^node\.([A-Za-z0-9_-]{1,64})\.output(?:\.([A-Za-z][A-Za-z0-9_-]{0,63}))?$/;
  const template = /\{\{\s*(input(?:\.[A-Za-z][A-Za-z0-9_-]{0,63})?|node\.[A-Za-z0-9_-]{1,64}\.output(?:\.[A-Za-z][A-Za-z0-9_-]{0,63})?)\s*\}\}/g;
  const positions = new Map(topologicalOrder(graph).map((id, index) => [id, index]));
  const inspect = (value: unknown, node: WorkflowNode): string => {
    if (typeof value === 'string') {
      const matches = [...value.matchAll(template)].map(match => match[1]);
      if ((value.includes('{{') || value.includes('}}')) && matches.length !== (value.match(/\{\{/g) || []).length) return `不能保存：节点“${node.label || node.id}”的工具参数只能引用 input 或上游 node 输出。`;
      for (const source of matches) {
        if (!sourcePattern.test(source) || !source.startsWith('node.')) continue;
        const match = nodeOutputPattern.exec(source);
        if (!match) return `不能保存：节点“${node.label || node.id}”的工具参数来源格式无效。`;
        const sourceId = match[1];
        if (!positions.has(sourceId)) return `不能保存：节点“${node.label || node.id}”引用的工具来源节点不存在。`;
        if (sourceId === node.id || (positions.get(sourceId) ?? 0) >= (positions.get(node.id) ?? 0)) return `不能保存：节点“${node.label || node.id}”的工具参数只能引用已经完成的上游节点。`;
        if (match[2]) {
          const sourceNode = graph.nodes.find(item => item.id === sourceId);
          const schemaText = sourceNode?.type === 'agent' && sourceNode.config.mode === 'model' ? nodeOutputSchemaText(sourceNode).trim() : '';
          let schema: Record<string, unknown> | null = null;
          try { schema = schemaText ? JSON.parse(schemaText) as Record<string, unknown> : null; } catch { schema = null; }
          const fields = Array.isArray(schema?.fields) ? schema.fields : [];
          const declared = fields.some(item => !!item && typeof item === 'object' && (item as Record<string, unknown>).name === match[2]);
          if (!schema || (!declared && schema.allow_extra !== true)) return `不能保存：节点“${node.label || node.id}”引用的工具字段未在来源节点中声明。`;
        }
      }
      return '';
    }
    if (Array.isArray(value)) {
      for (const item of value) { const issue = inspect(item, node); if (issue) return issue; }
      return '';
    }
    if (value && typeof value === 'object') {
      for (const item of Object.values(value)) { const issue = inspect(item, node); if (issue) return issue; }
    }
    return '';
  };
  for (const node of graph.nodes) {
    if (node.type !== 'mcp_read') continue;
    const raw = node.config.arguments_json;
    if (raw === undefined || raw === '') continue;
    let value: unknown = raw;
    if (typeof raw === 'string') {
      try { value = JSON.parse(raw); } catch { return `不能保存：节点“${node.label || node.id}”的工具参数不是有效 JSON。`; }
    }
    if (!value || typeof value !== 'object' || Array.isArray(value)) return `不能保存：节点“${node.label || node.id}”的工具参数必须是 JSON 对象。`;
    const issue = inspect(value, node);
    if (issue) return issue;
  }
  return '';
}
export function workflowConditionIssue(graph: WorkflowGraph): string {
  const operators = new Set(['exists', 'eq', 'neq', 'gt', 'gte', 'lt', 'lte', 'contains', 'in']);
  const sourcePattern = /^(?:input(?:\.[A-Za-z][A-Za-z0-9_-]{0,63})?|node\.[A-Za-z0-9_-]{1,64}\.output(?:\.[A-Za-z][A-Za-z0-9_-]{0,63})?)$/;
  const nodeOutputPattern = /^node\.([A-Za-z0-9_-]{1,64})\.output(?:\.([A-Za-z][A-Za-z0-9_-]{0,63}))?$/;
  const positions = new Map(topologicalOrder(graph).map((id, index) => [id, index]));
  for (const node of graph.nodes) {
    if (node.type !== 'condition') continue;
    const operator = node.config.operator === undefined ? 'exists' : node.config.operator;
    if (typeof operator !== 'string' || !operators.has(operator)) return `不能保存：节点“${node.label || node.id}”的条件运算符无效。`;
    const valueSource = node.config.value_source;
    if (valueSource !== undefined && valueSource !== '') {
      if (typeof valueSource !== 'string' || !sourcePattern.test(valueSource)) return `不能保存：节点“${node.label || node.id}”的判断值来源格式无效。`;
      if (valueSource.startsWith('node.')) {
        const match = nodeOutputPattern.exec(valueSource);
        if (!match) return `不能保存：节点“${node.label || node.id}”的判断值来源格式无效。`;
        const sourceId = match[1];
        if (!positions.has(sourceId)) return `不能保存：节点“${node.label || node.id}”引用的来源节点不存在。`;
        if (sourceId === node.id || (positions.get(sourceId) ?? 0) >= (positions.get(node.id) ?? 0)) return `不能保存：节点“${node.label || node.id}”只能引用已经完成的上游节点。`;
        if (match[2]) {
          const sourceNode = graph.nodes.find(item => item.id === sourceId);
          const schemaText = sourceNode?.type === 'agent' && sourceNode.config.mode === 'model' ? nodeOutputSchemaText(sourceNode).trim() : '';
          let schema: Record<string, unknown> | null = null;
          try { schema = schemaText ? JSON.parse(schemaText) as Record<string, unknown> : null; } catch { schema = null; }
          const fields = Array.isArray(schema?.fields) ? schema.fields : [];
          const declared = fields.some(item => !!item && typeof item === 'object' && (item as Record<string, unknown>).name === match[2]);
          if (!schema || (!declared && schema.allow_extra !== true)) return `不能保存：节点“${node.label || node.id}”引用的判断字段未在来源节点中声明。`;
        }
      }
    }
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
  const [providerPreflight, setProviderPreflight] = useState<ProviderPreflight | null>(null);
  const [connections, setConnections] = useState<Connection[]>([]);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [workflowConversationId, setWorkflowConversationId] = useState('');
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
  const [runHistory, setRunHistory] = useState<WorkflowRunSummary[]>([]);
  const [selectedTraceNodeId, setSelectedTraceNodeId] = useState('');
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
      const [values, availableTemplates, assistantValue, availableModels, availableConversations, availableConnections, preflight] = await Promise.all([
        api<Workflow[]>(`/assistants/${encodeURIComponent(assistantId)}/workflows`),
        api<WorkflowTemplate[]>('/workflow-templates'),
        api<Assistant>(`/assistants/${encodeURIComponent(assistantId)}`),
        api<ModelOption[]>('/models'),
        api<Conversation[]>(`/conversations?assistant_id=${encodeURIComponent(assistantId)}&archived=false`),
        api<Connection[]>('/connections'),
        api<ProviderPreflight>('/models/provider-preflight'),
      ]);
      setWorkflows(values);
      setTemplates(availableTemplates);
      setAssistant(assistantValue);
      setModels(availableModels);
      setProviderPreflight(preflight);
      setConnections(availableConnections);
      setConversations(availableConversations);
      setWorkflowConversationId(previous => availableConversations.some(item => item.id === previous) ? previous : (availableConversations[0]?.id || ''));
      const id = workflowId || selected?.id || values[0]?.id;
      if (id) await selectWorkflow(id, values);
      else { setSelected(null); setRunHistory([]); replaceGraph(cloneGraph(DEFAULT_WORKFLOW_GRAPH)); }
    } catch (cause) { setError(errorText(cause)); }
    finally { setLoading(false); }
  };

  const selectWorkflow = async (workflowId: string, list = workflows) => {
    setError('');
    try {
      const detail = await api<Workflow>(`/workflows/${encodeURIComponent(workflowId)}`);
      let history: WorkflowRunSummary[] = [];
      try { history = await api<WorkflowRunSummary[]>(`/workflows/${encodeURIComponent(workflowId)}/runs?limit=20`); } catch { /* Older servers can omit run history while the editor remains usable. */ }
      setSelected(detail); setWorkflows(list.map(item => item.id === detail.id ? { ...item, ...detail } : item));
      setName(detail.name); setDescription(detail.description); replaceGraph(graphFrom(detail));
      const nextGraph = graphFrom(detail); const first = topologicalOrder(nextGraph)[0] || nextGraph.nodes[0]?.id || ''; setSelectedNode(first); setEdgeSource(first); setEdgeTarget(nextGraph.nodes.find(node => node.id !== first)?.id || ''); setRunInput(Object.fromEntries(workflowFormFields(nextGraph).map(field => [field.id, field.type === 'number' ? '' : '']))); setRun(null); setSelectedTraceNodeId('');
      setRunHistory(history);
      setApprovalComment('');
    } catch (cause) { setError(errorText(cause)); }
  };

  useEffect(() => { void load(); }, [assistantId]); // eslint-disable-line react-hooks/exhaustive-deps

  const workflowFields = workflowFormFields(graph);
  useEffect(() => {
    setRunInput(current => {
      const next = Object.fromEntries(workflowFields.map(field => [field.id, Object.prototype.hasOwnProperty.call(current, field.id) ? current[field.id] : '']));
      const currentKeys = Object.keys(current);
      const nextKeys = Object.keys(next);
      if (currentKeys.length === nextKeys.length && currentKeys.every(key => Object.prototype.hasOwnProperty.call(next, key) && Object.is(current[key], next[key]))) return current;
      return next;
    });
  }, [graph]);

  const activeNode = graph.nodes.find(node => node.id === selectedNode) || graph.nodes[0];
  const activeNodeFields = activeNode ? workflowFormFields({ nodes: [activeNode], edges: [] }) : [];
  const activeModelId = nodeConfigText(activeNode, 'model_id', assistant?.default_model_id || '');
  const activeModel = models.find(model => model.id === activeModelId);
  const availableMcpTools = connections.flatMap(connection => {
    const binding = assistant?.connections.find(item => item.connection_id === connection.id);
    if (!binding) return [];
    return connection.tools.filter(tool => binding.allowed_tools.includes(tool.name)).map(tool => ({ value: `mcp__c${connection.id}__${tool.name}`, label: `${connection.name} · ${tool.name}`, description: tool.description }));
  });
  const hasModelNodes = graph.nodes.some(node => node.type === 'agent' && node.config.mode === 'model');
  const hasLiveTools = graph.nodes.some(node => node.type === 'mcp_read' && node.config.mode === 'live');
  const runIsDebug = run?.output?.debug === true;
  const runDebugNodeId = typeof run?.output?.node_id === 'string' ? run.output.node_id : '';
  const selectedTrace = run ? (run.trace.find(item => item.node_id === selectedTraceNodeId) || run.trace[run.trace.length - 1]) : undefined;
  const pendingApproval = run?.approvals?.find(item => item.status === 'pending') || (run?.approval?.status === 'pending' ? run.approval : null);
  const runStatusLabel = run?.status === 'awaiting_approval' ? '等待人工审批' : run?.status === 'failed' ? '运行失败' : run?.execution_mode === 'tool' ? '实时工具运行成功' : run?.synthetic ? '合成运行成功' : '模型运行成功';
  // Fitness routing may intentionally point at a separately approved 1007
  // target that is unavailable in a local catalog.  Workflow nodes still use
  // the assistant catalog, so use the independently checked Tab route as a
  // provider credential signal instead of showing a false warning when only
  // the fitness target is missing.
  const providerConfigured = providerPreflight?.router.fitness.provider_configured === true || providerPreflight?.router.tab.provider_configured === true;
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
      : workflowFormIssue(graph) || workflowArtifactIssue(graph) || workflowOutputSchemaIssue(graph) || workflowInputBindingsIssue(graph) || workflowKnowledgeIssue(graph) || workflowToolArgumentsIssue(graph) || workflowConditionIssue(graph);

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
    if (hasLiveTools && !workflowConversationId) { setError('实时工具节点必须绑定一个同一助手的运行会话。'); return; }
    setBusy(true); setError('');
    try {
      const next = await post<WorkflowRun>(`/workflows/${encodeURIComponent(selected.id)}/runs`, { input: runInput, ...(workflowConversationId ? { conversation_id: workflowConversationId } : {}) });
      setRun(next);
      setRunHistory(values => [workflowRunSummary(next), ...values.filter(item => item.id !== next.id)].slice(0, 20));
      setSelectedTraceNodeId(next.trace[next.trace.length - 1]?.node_id || '');
      setApprovalComment('');
      setNotice(next.status === 'awaiting_approval' ? '工作流已暂停，等待审批节点的明确决定。' : hasLiveTools ? '工作流运行完成，实时工具结果和来源证据已写入运行审计。' : hasModelNodes ? '工作流运行完成，智能体节点已使用发布版本中的模型配置。' : '合成测试完成，未调用外部模型或业务工具。');
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
      setRunHistory(values => [workflowRunSummary(next), ...values.filter(item => item.id !== next.id)].slice(0, 20));
      setSelectedTraceNodeId(next.trace[next.trace.length - 1]?.node_id || '');
      setApprovalComment('');
      setNotice(decision === 'approve' ? (next.status === 'awaiting_approval' ? '审批已通过，工作流已进入下一个审批点。' : '审批已通过，工作流继续执行并完成。') : '审批已拒绝，工作流已停止并保留审计记录。');
    } catch (cause) { setError(errorText(cause)); }
    finally { setBusy(false); }
  };

  const debugNode = async () => {
    if (!selected || !activeNode || activeNode.type !== 'agent' || nodeConfigText(activeNode, 'mode', 'synthetic') !== 'model' || !editable) return;
    if (dirty) { setError('请先保存当前草稿，再调试节点。'); return; }
    setBusy(true); setError('');
    try { const next = await post<WorkflowRun>(`/workflows/${encodeURIComponent(selected.id)}/nodes/${encodeURIComponent(activeNode.id)}/debug`, { input: runInput }); setRun(next); setRunHistory(values => [workflowRunSummary(next), ...values.filter(item => item.id !== next.id)].slice(0, 20)); setSelectedTraceNodeId(next.trace[next.trace.length - 1]?.node_id || activeNode.id); setNotice(`节点“${activeNode.label}”调试完成，使用的是当前草稿配置。`); }
    catch (cause) { setError(errorText(cause)); }
    finally { setBusy(false); }
  };

  const openRun = async (summary: WorkflowRunSummary) => {
    setBusy(true); setError('');
    try {
      const detail = await api<WorkflowRun>(`/workflow-runs/${encodeURIComponent(summary.id)}`);
      setRun(detail);
      setSelectedTraceNodeId(detail.trace[detail.trace.length - 1]?.node_id || '');
      setNotice(`已打开 ${new Date(summary.created_at).toLocaleString('zh-CN')} 的运行记录。`);
    } catch (cause) { setError(errorText(cause)); }
    finally { setBusy(false); }
  };

  const addNode = (type: WorkflowNodeType) => {
    if (!editable) return;
    const id = nodeId(type, graph.nodes); const node: WorkflowNode = { id, type, label: NODE_META[type].label, config: type === 'input' || type === 'agent' || type === 'knowledge_search' || type === 'mcp_read' ? { mode: 'synthetic' } : {} };
    const previous = graph.nodes.find(item => item.id === selectedNode) || graph.nodes[graph.nodes.length - 1];
    replaceGraph(insertWorkflowNode(graph, node, selectedNode));
    setSelectedNode(id); setEdgeSource(previous?.id || id); setEdgeTarget(id);
  };

  const duplicateNode = () => {
    if (!editable || !activeNode) return;
    const duplicate = duplicateWorkflowNode(activeNode, graph.nodes);
    replaceGraph(value => ({ ...value, nodes: [...value.nodes, duplicate] }));
    setSelectedNode(duplicate.id);
    setEdgeSource(activeNode.id);
    setEdgeTarget(duplicate.id);
    setNotice(`节点“${activeNode.label}”的副本已创建，请连接到流程。`);
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
        <div className={`workflow-boundary ${hasModelNodes || hasLiveTools ? 'workflow-boundary-model' : ''}`}><ShieldCheck size={15}/><span>{hasLiveTools ? '运行边界：实时工具节点只调用助手中已授权、已自动批准的只读 MCP 工具；连接凭据由服务端保管，结果会固定到本次运行的权限快照和来源证据。' : hasModelNodes ? '模型边界：智能体节点只调用助手已授权的模型目录；模型密钥不进入工作流图，知识、工具和审批节点仍按各自权限执行。' : '安全边界：当前节点只生成计划和状态，不访问生产数据、凭据、模型或外部业务系统。选择“模型执行”后才会调用已授权模型。'}</span></div>
        {hasModelNodes && providerPreflight && <div className={`workflow-model-readiness ${providerConfigured ? 'ready' : 'warning'}`} role="status"><span><CircleDot size={13}/><strong>{providerConfigured ? '模型线路已配置' : '模型线路未配置授权'}</strong></span><small>{providerConfigured ? '服务端已发现授权配置；运行时仍会按每个节点固定的公开模型和推理级别调用。' : '当前环境没有发现模型授权配置。可以继续搭建和发布，但运行时会被服务端拒绝，请先配置服务端模型线路。'}</small></div>}
        {hasLiveTools && <div className="workflow-runtime-select"><Field label="实时工具运行会话" hint="实时工具需要同一助手的会话权限快照；没有会话时只能保存和合成测试。"><select value={workflowConversationId} disabled={busy} onChange={event => setWorkflowConversationId(event.target.value)}><option value="">请选择运行会话</option>{conversations.map(item => <option key={item.id} value={item.id}>{item.title || '未命名会话'} · {new Date(item.updated_at).toLocaleString('zh-CN')}</option>)}</select></Field><small>运行会把会话当前的助手授权、连接版本和工具批准状态固定下来。</small></div>}
        {workflowFields.length > 0 && <section className="workflow-panel workflow-runtime-inputs"><div className="workflow-section-head"><div><h2>运行输入</h2><p>这些字段来自开始 / 输入或工作方法节点，发布后每次运行都会按此契约校验。</p></div><span>{workflowFields.length} 个字段</span></div><div className="workflow-runtime-input-grid">{workflowFields.map(field => <WorkflowFormControl key={field.id} field={field} value={runInput[field.id]} disabled={busy} onChange={(id, value) => setRunInput(current => ({ ...current, [id]: value }))} />)}</div></section>}<div className="workflow-canvas-card workflow-flowgram-card"><div className="workflow-section-head"><div><h2>流程画布</h2><p>拖拽节点、连接步骤，使用画布工具缩放和撤销；双击节点名称可直接编辑。</p></div><span>{graph.nodes.length} 节点 · {graph.edges.length} 条连线</span></div><Suspense fallback={<div className="zhixing-flowgram-loading">正在加载画布…</div>}><FlowGramWorkflowCanvas graph={graph} models={models} defaultModelId={assistant?.default_model_id} editable={editable} resetToken={`${canvasResetToken}:${busy ? 'busy' : 'ready'}:${editable ? 'editable' : 'readonly'}:${resetEpoch}`} onChange={handleCanvasGraphChange} onSelectNode={setSelectedNode} /></Suspense><div className="workflow-canvas-legend"><span><i className="legend-safe"/>节点执行边界</span><span><i className="legend-approval"/>人工审批点</span><span><i className="legend-draft"/>未发布修改</span></div></div>
        <div className="workflow-lower-grid"><section className="workflow-panel workflow-palette"><div className="workflow-section-head"><div><h2>添加能力节点</h2><p>点击能力节点添加到当前选中节点之后，再在右侧完成运行方式和模型配置。</p></div></div><div className="workflow-palette-grid">{(Object.keys(NODE_META) as WorkflowNodeType[]).map(type=><button type="button" key={type} disabled={!editable||busy} onClick={()=>addNode(type)}><span style={{ color: NODE_META[type].color }}><Plus size={14}/></span><span><strong>{NODE_META[type].label}</strong><small>{NODE_META[type].hint}</small></span></button>)}</div></section><section className="workflow-panel workflow-inspector"><div className="workflow-section-head"><div><h2>节点配置</h2><p>智能体节点可以绑定助手模型、角色和系统提示词；凭据永远不进入流程图。</p></div>{activeNode&&<><Button variant="ghost" disabled={!editable||busy} onClick={duplicateNode}><Copy size={14}/>复制</Button><Button variant="ghost" disabled={!editable||busy||graph.nodes.length<=1} onClick={removeNode}><Trash2 size={14}/>移除</Button></>}</div>{activeNode ? <div className="workflow-inspector-fields"><Field label="节点名称"><input value={activeNode.label} disabled={!editable||busy} onChange={event=>setGraph(value=>({...value,nodes:value.nodes.map(node=>node.id===activeNode.id?{...node,label:event.target.value}:node)}))} onBlur={()=>setCanvasResetToken(value=>value+1)}/></Field><Field label="节点类型"><select value={activeNode.type} disabled={!editable||busy} onChange={event=>{const type=event.target.value as WorkflowNodeType;replaceGraph(value=>({...value,nodes:value.nodes.map(node=>node.id===activeNode.id?{...node,type,label:NODE_META[type].label,config:type==='input'||type==='agent'||type==='knowledge_search'||type==='mcp_read'?{mode:'synthetic'}:{}}:node)}));}}>{(Object.keys(NODE_META) as WorkflowNodeType[]).map(type=><option key={type} value={type}>{NODE_META[type].label}</option>)}</select></Field>{activeNode.type==='agent'&&<><Field label="执行方式" hint="选择模型执行后，发布版本会按此节点配置调用已授权模型。"><select value={nodeConfigText(activeNode,'mode','synthetic')} disabled={!editable||busy} onChange={event=>updateActiveNodeConfig({mode:event.target.value},true)}><option value="synthetic">合成执行（不调用模型）</option><option value="model">模型执行（调用已选模型）</option></select></Field><Field label="角色定位" hint="用一句话说明这个智能体在流程中的职责。"><input value={nodeConfigText(activeNode,'role')} maxLength={200} disabled={!editable||busy} placeholder="例如：财务政策审核员" onChange={event=>updateActiveNodeConfig({role:event.target.value})}/></Field><Field label="节点任务说明" hint="这是当前节点要完成的具体任务。"><textarea rows={3} maxLength={1000} value={nodeConfigText(activeNode,'prompt')} disabled={!editable||busy} placeholder="例如：提取申请中的金额、部门和风险点" onChange={event=>updateActiveNodeConfig({prompt:event.target.value})}/></Field><Field label="系统提示词" hint="定义行为边界、输出格式和核验要求；会随发布版本固定。"><textarea rows={6} maxLength={4000} value={nodeConfigText(activeNode,'system_prompt')} disabled={!editable||busy} placeholder={assistant?.instructions||'写清楚角色、步骤、事实边界和输出格式'} onChange={event=>updateActiveNodeConfig({system_prompt:event.target.value})}/></Field><Field label="输入变量映射" hint="每行一项，例如：金额 = input.amount；结论 = node.review.output 或 node.review.output.decision。只允许引用工作流输入或已经完成的上游节点。"><textarea rows={4} maxLength={2000} value={workflowInputBindingsText(activeNode)} disabled={!editable||busy} placeholder="金额 = input.amount\n结论 = node.review.output.decision" onChange={event=>updateActiveNodeConfig({input_bindings:parseWorkflowInputBindings(event.target.value)})}/></Field><Field label="输出结构（JSON）" hint="可选。声明后模型必须返回符合约定的 JSON，下游可以引用 node.当前节点.output.字段。"><textarea rows={7} maxLength={4000} value={nodeOutputSchemaText(activeNode)} disabled={!editable||busy} placeholder={'{\n  "fields": [\n    {"name": "decision", "type": "text", "required": true},\n    {"name": "score", "type": "number", "required": true}\n  ]\n}'} onChange={event=>updateActiveNodeConfig({output_schema_json:event.target.value})}/></Field><div className="workflow-model-grid"><Field label="模型"><select value={activeModelId} disabled={!editable||busy||!models.length} onChange={event=>{const id=event.target.value;const selectedModel=models.find(model=>model.id===id);updateActiveNodeConfig({model_id:id||null,reasoning_level:selectedModel?.default_reasoning_level||null},true);}}><option value="">使用助手默认模型</option>{models.map(model=><option key={model.id} value={model.id} disabled={!model.available}>{model.label}{!model.available?'（未开通）':''}</option>)}{activeModelId&&!activeModel&&<option value={activeModelId}>当前模型（暂不可用）</option>}</select></Field><Field label="推理级别"><select value={nodeConfigText(activeNode,'reasoning_level',activeModel?.default_reasoning_level||'')} disabled={!editable||busy||!activeModel} onChange={event=>updateActiveNodeConfig({reasoning_level:event.target.value||null},true)}><option value="">模型默认</option>{activeModel?.reasoning_levels.map(level=><option key={level.id} value={level.id}>{level.label}</option>)}</select></Field></div><div className="workflow-model-advanced"><Field label="温度" hint="越低越稳定；会作为发布版本的一部分固定。"><input type="number" min={0} max={2} step={0.1} value={nodeConfigNumber(activeNode,'temperature',0.2).toFixed(1)} disabled={!editable||busy} onChange={event=>updateActiveNodeConfig({temperature:Math.max(0,Math.min(2,Number(event.target.value)||0))})}/></Field><Field label="最大输出" hint="限制单个节点的输出长度。"><input type="number" min={32} max={4096} step={32} value={nodeConfigNumber(activeNode,'max_tokens',512)} disabled={!editable||busy} onChange={event=>updateActiveNodeConfig({max_tokens:Math.max(32,Math.min(4096,Math.round(Number(event.target.value)||32)))})}/></Field><Field label="失败重试" hint="只对模型线路暂时失败或空响应重试，最多 3 次。"><input type="number" min={0} max={3} step={1} value={nodeConfigNumber(activeNode,'max_retries',0)} disabled={!editable||busy} onChange={event=>updateActiveNodeConfig({max_retries:Math.max(0,Math.min(3,Math.round(Number(event.target.value)||0)))})}/></Field><Field label="重试间隔（毫秒）" hint="采用递增间隔，最多 5000 毫秒。"><input type="number" min={0} max={5000} step={50} value={nodeConfigNumber(activeNode,'retry_backoff_ms',250)} disabled={!editable||busy} onChange={event=>updateActiveNodeConfig({retry_backoff_ms:Math.max(0,Math.min(5000,Math.round(Number(event.target.value)||0)))})}/></Field></div>{nodeConfigText(activeNode,'mode','synthetic')==='model'&&<div className="workflow-model-status"><Check size={14}/><span><strong>{activeModel?.label||'助手默认模型'}</strong><small>{dirty?'请先保存草稿，再调试当前节点。':activeModel?.available===false?'模型当前不可用，请重新选择。':'发布后运行会调用此模型；模型密钥由服务端管理。'}</small></span><Button variant="secondary" busy={busy} disabled={!editable||busy||dirty||!activeModel||activeModel.available===false} onClick={()=>void debugNode()}><Play size={13}/>调试此节点</Button></div>}</>}{(activeNode.type==='skill'||activeNode.type==='input')&&<><Field label={activeNode.type==='input'?'开始输入字段（JSON）':'输入字段（JSON）'} hint="定义手动运行时显示的字段；字段标识使用英文，choice 类型需要 options 数组。"><textarea rows={9} maxLength={4000} value={nodeFormFieldsText(activeNode)} disabled={!editable||busy} placeholder='[{"id":"topic","type":"text","label":"主题","required":true}]' onChange={event=>{const text=event.target.value;let parsed: unknown;try{parsed=JSON.parse(text);}catch{parsed=undefined;}updateActiveNodeConfig({form_fields_json:text,...(Array.isArray(parsed)?{form_fields:parsed}:{})});}}/></Field><div className="workflow-node-contract"><Check size={14}/><span>执行模式：<strong>结构化输入</strong><small>运行前会按这些字段生成表单，字段值会进入后续智能体、条件和产物节点。</small></span></div></>}{activeNode.type==='knowledge_search'&&<><Field label="检索问题" hint="支持 {{input.topic}} 或 {{node.review.output.decision}} 模板；运行时只检索助手已授权且启用的知识库。"><textarea rows={4} maxLength={2000} value={nodeConfigText(activeNode,'query')} disabled={!editable||busy} placeholder="例如：请检索 {{input.topic}} 的质量标准" onChange={event=>updateActiveNodeConfig({query:event.target.value})}/></Field><div className="workflow-model-grid"><Field label="召回条数" hint="最多 20 条，结果会带文件版本和来源摘要。"><input type="number" min={1} max={20} step={1} value={nodeConfigNumber(activeNode,'top_k',6)} disabled={!editable||busy} onChange={event=>updateActiveNodeConfig({top_k:Math.max(1,Math.min(20,Number(event.target.value)||1))})}/></Field><Field label="检索模式"><select value={nodeConfigText(activeNode,'retrieval_mode','hybrid')} disabled={!editable||busy} onChange={event=>updateActiveNodeConfig({retrieval_mode:event.target.value})}><option value="hybrid">混合检索</option><option value="bm25">关键词检索</option><option value="vector">向量检索</option></select></Field></div><Field label="上下文长度" hint="传给后续智能体的检索上下文上限，最多 12000 字符。"><input type="number" min={0} max={12000} step={500} value={nodeConfigNumber(activeNode,'context_chars',12000)} disabled={!editable||busy} onChange={event=>updateActiveNodeConfig({context_chars:Math.max(0,Math.min(12000,Number(event.target.value)||0))})}/></Field><div className="workflow-node-contract"><Check size={14}/><span>执行模式：<strong>授权知识检索</strong><small>节点不保存知识库编号；运行时读取当前助手已授权、已启用的知识库，并保留文件版本、摘要和来源证据。</small></span></div></>}{activeNode.type==='mcp_read'&&<><Field label="执行方式" hint="实时调用会使用运行会话中助手已自动批准的只读 MCP 工具；合成模式只用于画布验证。"><select value={nodeConfigText(activeNode,'mode','synthetic')} disabled={!editable||busy} onChange={event=>updateActiveNodeConfig({mode:event.target.value},true)}><option value="synthetic">合成执行（不访问业务系统）</option><option value="live">实时调用（授权只读工具）</option></select></Field><Field label="工具名称" hint="只显示当前助手已绑定的连接工具；连接凭据和服务地址由服务端管理。"><select value={nodeConfigText(activeNode,'tool_name')} disabled={!editable||busy} onChange={event=>updateActiveNodeConfig({tool_name:event.target.value},true)}><option value="">请选择已授权工具</option>{availableMcpTools.map(tool=><option key={tool.value} value={tool.value}>{tool.label}</option>)}{nodeConfigText(activeNode,'tool_name')&&!availableMcpTools.some(tool=>tool.value===nodeConfigText(activeNode,'tool_name'))&&<option value={nodeConfigText(activeNode,'tool_name')}>当前工具（已不可用）</option>}</select></Field><Field label="工具参数模板" hint="JSON 对象；字符串值可以引用 {{input.field}} 或 {{node.review.output.decision}}。凭据不能写入流程图。"><textarea rows={7} maxLength={4000} value={nodeToolArgumentsText(activeNode)} disabled={!editable||busy} onChange={event=>updateActiveNodeConfig({arguments_json:event.target.value})}/></Field><Field label="超时时间（秒）"><input type="number" min={1} max={60} step={1} value={nodeConfigNumber(activeNode,'timeout_seconds',30)} disabled={!editable||busy} onChange={event=>updateActiveNodeConfig({timeout_seconds:Math.max(1,Math.min(60,Number(event.target.value)||1))})}/></Field><div className="workflow-node-contract"><Check size={14}/><span>执行模式：<strong>{nodeConfigText(activeNode,'mode','synthetic')==='live'?'授权只读工具':'合成工具节点'}</strong><small>{nodeConfigText(activeNode,'mode','synthetic')==='live'?'运行时校验助手绑定、自动批准状态和连接版本；工具返回的结构化来源会写入运行审计。':'不会访问外部连接，适合先搭建并验证流程。'}</small></span></div></>}{activeNode.type==='condition'&&<><Field label="判断字段" hint="读取工作流输入或上游模型输出字段，例如 input.amount / node.review.output.score。"><input value={nodeConfigText(activeNode,'field')} maxLength={64} disabled={!editable||busy} placeholder="例如：amount" onChange={event=>updateActiveNodeConfig({field:event.target.value})}/></Field><Field label="判断值来源" hint="留空时读取上面的工作流输入字段；也可以引用已完成的模型输出，例如 node.review.output.score。"><input value={nodeConfigText(activeNode,'value_source')} maxLength={200} disabled={!editable||busy} placeholder="留空，或填写 input.amount / node.review.output.score" onChange={event=>updateActiveNodeConfig({value_source:event.target.value})}/></Field><div className="workflow-model-grid"><Field label="运算符"><select value={nodeConfigText(activeNode,'operator','exists')} disabled={!editable||busy} onChange={event=>updateActiveNodeConfig({operator:event.target.value})}><option value="exists">存在</option><option value="eq">等于</option><option value="neq">不等于</option><option value="gt">大于</option><option value="gte">大于等于</option><option value="lt">小于</option><option value="lte">小于等于</option><option value="contains">包含</option><option value="in">属于列表</option></select></Field><Field label="比较值"><input value={nodeConfigValueText(activeNode,'value')} disabled={!editable||busy} placeholder="例如：5000" onChange={event=>updateActiveNodeConfig({value:conditionValue(event.target.value)})}/></Field></div><div className="workflow-model-grid"><Field label="满足时标签" hint="条件为真时选择同名连线。"><input value={nodeConfigText(activeNode,'true_label')} maxLength={120} disabled={!editable||busy} placeholder="例如：通过" onChange={event=>updateActiveNodeConfig({true_label:event.target.value})}/></Field><Field label="不满足时标签" hint="条件为假时选择同名连线。"><input value={nodeConfigText(activeNode,'false_label')} maxLength={120} disabled={!editable||busy} placeholder="例如：不通过" onChange={event=>updateActiveNodeConfig({false_label:event.target.value})}/></Field></div><div className="workflow-node-contract"><Check size={14}/><span>执行模式：<strong>确定性条件分支</strong><small>分支只读取已校验的工作流输入或上游模型结构化输出；未选中的出口会记录为跳过，不会继续执行。</small></span></div></>} {activeNode.type==='approval'&&<><Field label="审批角色" hint="用于运行审计，写明谁负责对本节点做出明确决定。"><input value={nodeConfigText(activeNode,'role')} maxLength={120} disabled={!editable||busy} placeholder="例如：部门负责人" onChange={event=>updateActiveNodeConfig({role:event.target.value})}/></Field><div className="workflow-node-contract"><Check size={14}/><span>执行模式：<strong>人工决策</strong><small>运行到此节点会暂停，只有明确批准或拒绝后才会继续，并保留决定人与审批说明。</small></span></div></>}{activeNode.type==='artifact'&&<><div className="workflow-model-grid"><Field label="产物格式"><select value={nodeConfigText(activeNode,'format','structured')} disabled={!editable||busy} onChange={event=>updateActiveNodeConfig({format:event.target.value})}><option value="structured">结构化预览</option><option value="xlsx">Excel 表格</option><option value="csv">CSV 文本</option><option value="json">JSON 数据</option></select></Field><Field label="输出字段" hint="用逗号分隔；留空时包含全部运行输入。"><input value={nodeArtifactColumnsText(activeNode)} disabled={!editable||busy} placeholder="例如：purpose, amount, date" onChange={event=>updateActiveNodeConfig({columns:parseArtifactColumnsText(event.target.value)})}/></Field></div><div className="workflow-node-contract"><Check size={14}/><span>执行模式：<strong>受控产物预览</strong><small>发布后按指定字段生成可审计的结构化预览；当前版本不会写入外部文件系统。</small></span></div></>}{activeNode.type!=='agent'&&activeNode.type!=='knowledge_search'&&activeNode.type!=='condition'&&activeNode.type!=='skill'&&activeNode.type!=='approval'&&activeNode.type!=='artifact'&&activeNode.type!=='input'&&<div className="workflow-node-contract"><Check size={14}/><span>执行模式：<strong>受控本地节点</strong><small>知识、工具、审批和产物节点按服务端权限与审计契约执行。</small></span></div>}{activeNodeFields.length>0&&<div className="workflow-form-preview"><div className="workflow-form-preview-head"><strong>结构化表单字段</strong><small>运行输入</small></div>{activeNodeFields.map(field=><WorkflowFormControl key={field.id} field={field} value={runInput[field.id]} disabled={!editable||busy} onChange={(id, value)=>setRunInput(current=>({...current,[id]:value}))}/>)}</div>}</div> : <p className="section-help">从画布选择一个节点开始配置。</p>}</section></div>
        <section className="workflow-panel workflow-edge-panel"><div className="workflow-section-head"><div><h2>连接步骤</h2><p>从一个节点指向另一个节点；条件分支可用连线标签选择出口，服务端会拒绝循环和孤立节点。</p></div></div><div className="workflow-edge-form"><select aria-label="连线起点" value={edgeSource} disabled={!editable||busy} onChange={event=>setEdgeSource(event.target.value)}>{graph.nodes.map(node=><option key={node.id} value={node.id}>{node.label}</option>)}</select><ChevronRight size={15}/><select aria-label="连线终点" value={edgeTarget} disabled={!editable||busy} onChange={event=>setEdgeTarget(event.target.value)}>{graph.nodes.map(node=><option key={node.id} value={node.id}>{node.label}</option>)}</select><Button variant="secondary" disabled={!editable||busy||!edgeSource||!edgeTarget||edgeSource===edgeTarget} onClick={addEdge}><Plus size={14}/>添加连线</Button></div><div className="workflow-edge-list">{graph.edges.map(edge=><span key={`${edge.source}-${edge.target}`}><span>{graph.nodes.find(node=>node.id===edge.source)?.label}<ChevronRight size={12}/>{graph.nodes.find(node=>node.id===edge.target)?.label}</span><input aria-label={`连线标签 ${edge.source} 到 ${edge.target}`} value={edge.label||''} placeholder="分支标签" disabled={!editable||busy} onChange={event=>setGraph(value=>({...value,edges:value.edges.map(item=>item.source===edge.source&&item.target===edge.target?{...item,label:event.target.value}:item)}))}/></span>)}</div></section>
        <section className="workflow-panel workflow-versions"><div className="workflow-section-head"><div><h2>版本与回滚</h2><p>已发布版本不可变；回滚只建立新草稿，必须再次人工发布。</p></div></div><div className="workflow-version-list">{selected.versions.map(version=><div key={version.version} className={`workflow-version-row ${version.version===selected.published_version?'current':''}`}><span className="workflow-version-number">v{version.version}</span><span><strong>{version.status==='published'?'已发布':version.status==='draft'?'草稿':'历史版本'}</strong><small>{version.graph_hash.slice(0,12)} · {new Date(version.created_at).toLocaleString('zh-CN')}</small></span>{editable&&version.version!==selected.draft_version&&<Button variant="ghost" disabled={busy} onClick={()=>void rollback(version.version)}><RotateCcw size={13}/>回滚为草稿</Button>}</div>)}</div></section>
        <section className="workflow-panel workflow-run-history"><div className="workflow-section-head"><div><h2>运行记录</h2><p>保留最近 20 次运行和节点调试；点击记录可重新查看完整轨迹与审计摘要。</p></div><span>{runHistory.length} 条记录</span></div>{runHistory.length ? <div className="workflow-run-history-list">{runHistory.map(item => <button type="button" key={item.id} className={`workflow-run-history-item ${run?.id === item.id ? 'selected' : ''}`} disabled={busy} aria-current={run?.id === item.id ? 'true' : undefined} onClick={() => void openRun(item)}><span className="workflow-run-history-icon"><History size={14}/></span><span><strong>{workflowRunHistoryLabel(item)} · v{item.version}</strong><small>{new Date(item.created_at).toLocaleString('zh-CN')} · {item.trace_count} 个节点{item.approval_node_id ? ` · 等待 ${item.approval_node_id} 审批` : ''}{item.error ? ` · ${item.error}` : ''}</small></span><ChevronRight size={14}/></button>)}</div> : <p className="section-help">发布后运行或调试节点，记录会出现在这里。</p>}</section>
        {run&&<section className={`workflow-run-card ${run.synthetic && run.execution_mode !== 'tool' ? '' : 'workflow-run-model'} ${run.status === 'awaiting_approval' ? 'workflow-run-awaiting' : ''}`}><div><span className="workflow-run-pill">{run.status === 'awaiting_approval' ? <ShieldCheck size={13}/> : <Check size={13}/>} {runIsDebug ? '节点调试成功' : runStatusLabel}</span><strong>{runIsDebug ? `已调试草稿节点${runDebugNodeId ? ` · ${runDebugNodeId}` : ''}` : `已运行发布版本 v${run.version}`}</strong><small>{runIsDebug ? `草稿版本 v${run.version} · ` : ''}图哈希 {run.graph_hash.slice(0,16)} · 权限摘要 {run.permission_digest.slice(0,16)}</small>{run.tool_evidence_records?.length ? <small>已登记 {run.tool_evidence_records.length} 组只读工具来源证据</small> : null}{pendingApproval&&<div className="workflow-run-approval"><span><strong>审批节点：{pendingApproval.node_id}</strong><small>明确选择后才会继续，决定会写入运行审计。</small></span><input aria-label="审批说明" maxLength={1000} value={approvalComment} placeholder="可选：写下审批依据" onChange={event=>setApprovalComment(event.target.value)}/><div><Button variant="secondary" disabled={busy} onClick={()=>void decideApproval(pendingApproval, 'approve')}>批准继续</Button><Button variant="ghost" disabled={busy} onClick={()=>void decideApproval(pendingApproval, 'reject')}>拒绝运行</Button></div></div>}</div><div className="workflow-run-trace-column"><div className="workflow-run-trace">{run.trace.map(item=><button type="button" className={`workflow-run-trace-item ${item.output_preview ? 'has-output' : ''} ${selectedTrace?.node_id === item.node_id ? 'selected' : ''}`} key={`${item.sequence}-${item.node_id}`} title={item.output_preview || undefined} aria-label={`查看节点 ${item.node_id} 的运行详情`} aria-pressed={selectedTrace?.node_id === item.node_id} onClick={()=>{setSelectedTraceNodeId(item.node_id); setSelectedNode(item.node_id);}}><span><CircleDot size={12}/>{item.node_id}{item.model_label&&<small>{item.model_label}</small>}{item.model_retries ? <small>重试 {item.model_retries} 次</small> : null}{item.tool_call&&<small>{item.evidence_count || 0} 条证据</small>}{item.structured_output&&<small>结构化输出</small>}<small>{item.status}</small></span>{item.output_preview&&<em>{item.output_preview}</em>}</button>)}</div>{selectedTrace&&<div className="workflow-run-detail" aria-live="polite"><div className="workflow-run-detail-head"><strong>节点运行详情</strong><span>{selectedTrace.node_id} · {selectedTrace.status}</span></div><div className="workflow-run-detail-grid">{selectedTrace.model_label&&<span><small>模型</small><strong>{selectedTrace.model_label}</strong></span>}{selectedTrace.model_id&&<span><small>模型标识</small><strong>{selectedTrace.model_id}</strong></span>}{selectedTrace.role&&<span><small>角色定位</small><strong>{selectedTrace.role}</strong></span>}{selectedTrace.reasoning_level&&<span><small>推理级别</small><strong>{selectedTrace.reasoning_level}</strong></span>}{selectedTrace.model_attempts !== undefined&&<span><small>模型尝试</small><strong>{selectedTrace.model_attempts} 次</strong></span>}{selectedTrace.model_retries !== undefined&&<span><small>失败重试</small><strong>{selectedTrace.model_retries} 次</strong></span>}{selectedTrace.tool_name&&<span><small>工具</small><strong>{selectedTrace.tool_name}</strong></span>}{selectedTrace.evidence_count !== undefined&&<span><small>来源证据</small><strong>{selectedTrace.evidence_count} 条</strong></span>}</div>{selectedTrace.input_bindings?.length ? <div className="workflow-run-detail-block"><small>输入变量</small><span>{selectedTrace.input_bindings.map(binding=><code key={`${binding.key}-${binding.source}`}>{binding.key} ← {binding.source}</code>)}</span></div> : null}{selectedTrace.selected_targets?.length ? <div className="workflow-run-detail-block"><small>分支出口</small><span>{selectedTrace.selected_targets.join('、')}</span></div> : null}{selectedTrace.structured_fields?.length ? <div className="workflow-run-detail-block"><small>结构化字段</small><span>{selectedTrace.structured_fields.join('、')}</span></div> : null}{selectedTrace.output_preview&&<div className="workflow-run-detail-block"><small>节点输出</small><pre>{selectedTrace.output_preview}</pre></div>}{selectedTrace.error_code&&<div className="workflow-run-detail-block workflow-run-detail-error"><small>错误编码</small><span>{selectedTrace.error_code}</span></div>}</div>}</div></section>}
      </>}</section>
    </div>
  </div>;
}
