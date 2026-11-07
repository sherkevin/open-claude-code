import { describe, expect, it } from 'vitest';
import { DEFAULT_WORKFLOW_GRAPH, graphHasCycle, topologicalOrder, workflowFormFields } from './WorkflowPage';

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

  it('keeps the visual contract explicitly synthetic', () => {
    expect(DEFAULT_WORKFLOW_GRAPH.nodes.every(node => node.config.mode === 'synthetic' || node.type === 'approval')).toBe(true);
    expect(DEFAULT_WORKFLOW_GRAPH.nodes.map(node => node.type)).toEqual(['agent', 'knowledge_search', 'approval']);
  });

  it('exposes declared template fields without accepting arbitrary secret inputs', () => {
    const fields = workflowFormFields({ nodes: [{ id: 'form', type: 'skill', label: '表单', config: { form_fields: [{ id: 'amount', type: 'number', label: '金额', required: true }] } }], edges: [] });
    expect(fields).toEqual([{ id: 'amount', type: 'number', label: '金额', required: true, options: undefined }]);
  });
});
