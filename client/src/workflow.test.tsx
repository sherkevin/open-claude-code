import { describe, expect, it } from 'vitest';
import { DEFAULT_WORKFLOW_GRAPH, graphHasCycle, graphHasIsolatedNode, parseWorkflowInputBindings, topologicalOrder, workflowFormFields, workflowInputBindingsIssue, workflowInputBindingsText } from './WorkflowPage';

describe('low-code workflow canvas contracts', () => {
  it('keeps the default business flow acyclic and ordered', () => {
    expect(topologicalOrder(DEFAULT_WORKFLOW_GRAPH)).toEqual(['agent', 'knowledge', 'approval']);
    expect(graphHasCycle(DEFAULT_WORKFLOW_GRAPH)).toBe(false);
  });

  it('detects a cycle before the client submits a draft', () => {
    const graph = { ...DEFAULT_WORKFLOW_GRAPH, edges: [...DEFAULT_WORKFLOW_GRAPH.edges, { source: 'approval', target: 'agent', label: '回流' }] };
    expect(graphHasCycle(graph)).toBe(true);
    expect(topologicalOrder(graph)).toHaveLength(0);
  });

  it('flags disconnected nodes before the client submits a draft', () => {
    const graph = { ...DEFAULT_WORKFLOW_GRAPH, nodes: [...DEFAULT_WORKFLOW_GRAPH.nodes, { id: 'artifact-1', type: 'artifact' as const, label: '未连接产物', config: {} }] };
    expect(graphHasIsolatedNode(graph)).toBe(true);
    expect(graphHasIsolatedNode(DEFAULT_WORKFLOW_GRAPH)).toBe(false);
  });

  it('keeps the visual contract explicitly synthetic', () => {
    expect(DEFAULT_WORKFLOW_GRAPH.nodes.every(node => node.config.mode === 'synthetic' || node.type === 'approval')).toBe(true);
    expect(DEFAULT_WORKFLOW_GRAPH.nodes.map(node => node.type)).toEqual(['agent', 'knowledge_search', 'approval']);
  });

  it('exposes declared template fields without accepting arbitrary secret inputs', () => {
    const fields = workflowFormFields({ nodes: [{ id: 'form', type: 'skill', label: '表单', config: { form_fields: [{ id: 'amount', type: 'number', label: '金额', required: true }] } }], edges: [] });
    expect(fields).toEqual([{ id: 'amount', type: 'number', label: '金额', required: true, options: undefined }]);
  });

  it('round-trips explicit node input bindings in the editor format', () => {
    const node = { id: 'review', type: 'agent' as const, label: '审核', config: { input_bindings: [{ key: 'amount', source: 'input.amount' }, { key: 'prior', source: 'node.plan.output' }] } };
    expect(workflowInputBindingsText(node)).toBe('amount = input.amount\nprior = node.plan.output');
    expect(parseWorkflowInputBindings(workflowInputBindingsText(node))).toEqual(node.config.input_bindings);
    expect(workflowInputBindingsIssue({ nodes: [node, { id: 'plan', type: 'agent', label: '计划', config: {} }], edges: [{ source: 'plan', target: 'review', label: '结论' }] })).toBe('');
    expect(workflowInputBindingsIssue({ nodes: [{ ...node, config: { input_bindings: [{ key: 'amount', source: 'node.missing.output' }] } }], edges: [] })).toContain('来源节点不存在');
  });
});
