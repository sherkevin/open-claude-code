import { describe, expect, it } from 'vitest';
import { DEFAULT_WORKFLOW_GRAPH, graphHasCycle, graphHasIsolatedNode, nodeOutputSchemaText, parseWorkflowInputBindings, topologicalOrder, workflowConditionIssue, workflowFormFields, workflowInputBindingsIssue, workflowInputBindingsText, workflowKnowledgeIssue, workflowOutputSchemaIssue } from './WorkflowPage';

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

  it('validates structured agent outputs and field bindings', () => {
    const review = { id: 'review', type: 'agent' as const, label: '审核', config: { mode: 'model', output_schema_json: '{"fields":[{"name":"decision","type":"text","required":true}]}' } };
    const explain = { id: 'explain', type: 'agent' as const, label: '说明', config: { input_bindings: [{ key: 'decision', source: 'node.review.output.decision' }] } };
    expect(nodeOutputSchemaText(review)).toContain('decision');
    expect(workflowOutputSchemaIssue({ nodes: [review, explain], edges: [{ source: 'review', target: 'explain', label: '传递' }] })).toBe('');
    expect(workflowInputBindingsIssue({ nodes: [review, explain], edges: [{ source: 'review', target: 'explain', label: '传递' }] })).toBe('');
    expect(workflowInputBindingsIssue({ nodes: [{ ...review, config: {} }, explain], edges: [{ source: 'review', target: 'explain', label: '传递' }] })).toContain('已接入模型');
    expect(workflowOutputSchemaIssue({ nodes: [{ ...review, config: { output_schema_json: '{"fields":[{"name":"score","type":"money"}]}' } }, explain], edges: [{ source: 'review', target: 'explain', label: '传递' }] })).toContain('输出字段类型无效');
  });

  it('validates knowledge query templates and bounded retrieval controls', () => {
    const graph = {
      nodes: [
        { id: 'plan', type: 'agent' as const, label: '计划', config: {} },
        { id: 'lookup', type: 'knowledge_search' as const, label: '检索', config: { query: '请查找 {{node.plan.output}}', top_k: 4, retrieval_mode: 'bm25', context_chars: 9000 } },
      ],
      edges: [{ source: 'plan', target: 'lookup', label: '依据' }],
    };
    expect(workflowKnowledgeIssue(graph)).toBe('');
    expect(workflowKnowledgeIssue({ ...graph, nodes: [{ ...graph.nodes[0] }, { ...graph.nodes[1], config: { query: '{{node.future.output}}' } }], edges: graph.edges })).toContain('来源节点不存在');
    expect(workflowKnowledgeIssue({ ...graph, nodes: [{ ...graph.nodes[0] }, { ...graph.nodes[1], config: { top_k: 21 } }], edges: graph.edges })).toContain('召回条数');
  });

  it('requires labeled exits for a condition branch', () => {
    const graph = {
      nodes: [{ id: 'gate', type: 'condition' as const, label: '判断', config: { field: 'amount', operator: 'gt', value: 5000 } }, { id: 'next', type: 'artifact' as const, label: '下一步', config: {} }, { id: 'other', type: 'artifact' as const, label: '另一步', config: {} }],
      edges: [{ source: 'gate', target: 'next', label: '' }, { source: 'gate', target: 'other', label: '' }],
    };
    expect(workflowConditionIssue(graph)).toContain('填写连线标签');
    expect(workflowConditionIssue({ ...graph, edges: [{ ...graph.edges[0], label: 'high' }, { ...graph.edges[1], label: 'low' }] })).toBe('');
  });

  it('validates condition branches that read structured agent fields', () => {
    const review = { id: 'review', type: 'agent' as const, label: '评分', config: { mode: 'model', output_schema_json: '{"fields":[{"name":"score","type":"number","required":true}]}' } };
    const gate = { id: 'gate', type: 'condition' as const, label: '判断', config: { value_source: 'node.review.output.score', operator: 'gte', value: 0.8 } };
    const graph = { nodes: [review, gate, { id: 'pass', type: 'artifact' as const, label: '通过', config: {} }], edges: [{ source: 'review', target: 'gate', label: '完成' }, { source: 'gate', target: 'pass', label: '通过' }] };
    expect(workflowConditionIssue(graph)).toBe('');
    expect(workflowConditionIssue({ ...graph, nodes: [review, { ...gate, config: { ...gate.config, value_source: 'node.review.output.decision' } }, graph.nodes[2]] })).toContain('判断字段未在来源节点中声明');
    expect(workflowConditionIssue({ ...graph, nodes: [review, { ...gate, config: { ...gate.config, value_source: 'node.missing.output.score' } }, graph.nodes[2]] })).toContain('来源节点不存在');
  });
});
