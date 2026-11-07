import { describe, expect, it } from 'vitest';
import { DEFAULT_WORKFLOW_GRAPH, changeWorkflowNodeType, duplicateWorkflowNode, graphHasCycle, graphHasIsolatedNode, insertWorkflowNode, nodeOutputSchemaText, parseWorkflowInputBindings, serializeWorkflowOutputSchema, topologicalOrder, workflowArtifactIssue, workflowBindingSourceOptions, workflowConditionIssue, workflowConfigKeyLabel, workflowErrorRecoveryHint, workflowFormFields, workflowFormIssue, workflowInputBindingsIssue, workflowInputBindingsText, workflowKnowledgeIssue, workflowNodeConfigComparison, workflowOutputReferenceBinding, workflowOutputSchemaComparison, workflowOutputSchemaIssue, workflowOutputSchemaDraft, workflowRunHistoryLabel, workflowRunInput, workflowRunNodeComparison, workflowRunReferenceValue, workflowRunSummary, workflowToolArgumentsIssue, workflowValuePreview } from './WorkflowPage';

describe('low-code workflow canvas contracts', () => {
  it('keeps the default business flow acyclic and ordered', () => {
    expect(topologicalOrder(DEFAULT_WORKFLOW_GRAPH)).toEqual(['start', 'agent', 'knowledge', 'approval']);
    expect(graphHasCycle(DEFAULT_WORKFLOW_GRAPH)).toBe(false);
  });

  it('detects a cycle before the client submits a draft', () => {
    const graph = { ...DEFAULT_WORKFLOW_GRAPH, edges: [...DEFAULT_WORKFLOW_GRAPH.edges, { source: 'approval', target: 'agent', label: '回流' }] };
    expect(graphHasCycle(graph)).toBe(true);
    expect(topologicalOrder(graph)).toEqual(['start']);
  });

  it('flags disconnected nodes before the client submits a draft', () => {
    const graph = { ...DEFAULT_WORKFLOW_GRAPH, nodes: [...DEFAULT_WORKFLOW_GRAPH.nodes, { id: 'artifact-1', type: 'artifact' as const, label: '未连接产物', config: {} }] };
    expect(graphHasIsolatedNode(graph)).toBe(true);
    expect(graphHasIsolatedNode(DEFAULT_WORKFLOW_GRAPH)).toBe(false);
  });

  it('keeps the visual contract explicitly synthetic', () => {
    expect(DEFAULT_WORKFLOW_GRAPH.nodes.every(node => node.config.mode === 'synthetic' || node.type === 'approval')).toBe(true);
    expect(DEFAULT_WORKFLOW_GRAPH.nodes.map(node => node.type)).toEqual(['input', 'agent', 'knowledge_search', 'approval']);
  });

  it('exposes declared template fields without accepting arbitrary secret inputs', () => {
    const fields = workflowFormFields({ nodes: [{ id: 'form', type: 'skill', label: '表单', config: { form_fields: [{ id: 'amount', type: 'number', label: '金额', required: true }] } }], edges: [] });
    expect(fields).toEqual([{ id: 'amount', type: 'number', label: '金额', required: true, options: undefined }]);
  });

  it('exposes fields declared by the explicit start input node', () => {
    const fields = workflowFormFields({ nodes: [{ id: 'start', type: 'input', label: '开始 / 输入', config: { form_fields: [{ id: 'topic', type: 'text', label: '主题', required: true }] } }], edges: [] });
    expect(fields).toEqual([{ id: 'topic', type: 'text', label: '主题', required: true, options: undefined }]);
  });

  it('duplicates a node without sharing config or copying edges', () => {
    const source = { id: 'review', type: 'agent' as const, label: '审核', config: { mode: 'model', role: '审核员', system_prompt: '严谨核验', __ui: { position: { x: 10, y: 20 } } } };
    const copy = duplicateWorkflowNode(source, [source]);
    expect(copy.id).toBe('agent-1');
    expect(copy.label).toBe('审核 副本');
    expect(copy.config.role).toBe('审核员');
    expect(copy.config.system_prompt).toBe('严谨核验');
    expect(copy.config.__ui).toEqual({ position: { x: 58, y: 68 } });
    expect(source.config.__ui).toEqual({ position: { x: 10, y: 20 } });
    expect(copy).not.toBe(source);
  });

  it('changes node type without losing the custom label or canvas position', () => {
    const source = { id: 'review', type: 'agent' as const, label: '政策审核', config: { mode: 'model', role: '审核员', system_prompt: '严谨核验', __ui: { position: { x: 120, y: 240 }, collapsed: false } } };
    const changed = changeWorkflowNodeType(source, 'knowledge_search');
    expect(changed.label).toBe('政策审核');
    expect(changed.config).toEqual({ mode: 'synthetic', __ui: { position: { x: 120, y: 240 }, collapsed: false } });
    expect(source.config).toEqual({ mode: 'model', role: '审核员', system_prompt: '严谨核验', __ui: { position: { x: 120, y: 240 }, collapsed: false } });
  });

  it('offers only runtime inputs and completed upstream output fields as binding sources', () => {
    const graph = {
      nodes: [
        { id: 'start', type: 'input' as const, label: '开始', config: { form_fields: [{ id: 'topic', type: 'text', label: '主题', required: true }] } },
        { id: 'review', type: 'agent' as const, label: '审核', config: { mode: 'model', output_schema_json: '{"fields":[{"name":"decision","type":"text","required":true}]}' } },
        { id: 'explain', type: 'agent' as const, label: '说明', config: {} },
        { id: 'future', type: 'agent' as const, label: '后续节点', config: {} },
      ],
      edges: [{ source: 'start', target: 'review', label: '输入' }, { source: 'review', target: 'explain', label: '结果' }, { source: 'explain', target: 'future', label: '继续' }],
    };
    const options = workflowBindingSourceOptions(graph, 'explain');
    expect(options.map(item => item.value)).toEqual(['input', 'input.topic', 'node.start.output', 'node.review.output', 'node.review.output.decision']);
    expect(options.some(item => item.value.includes('future'))).toBe(false);
  });

  it('round-trips the structured output schema editor contract', () => {
    const node = { id: 'review', type: 'agent' as const, label: '审核', config: { output_schema_json: serializeWorkflowOutputSchema([{ name: 'decision', type: 'text' as const, required: true, description: '审批结论' }], false) } };
    expect(workflowOutputSchemaDraft(node)).toEqual({ valid: true, raw: node.config.output_schema_json, fields: [{ name: 'decision', type: 'text', required: true, description: '审批结论' }], allowExtra: false });
    expect(workflowOutputSchemaDraft({ ...node, config: { output_schema_json: '{bad' } })).toMatchObject({ valid: false, raw: '{bad' });
    expect(serializeWorkflowOutputSchema([], false)).toBe('');
  });

  it('creates a safe downstream binding from a structured output field', () => {
    const graph = {
      nodes: [{ id: 'start', type: 'input' as const, label: '开始', config: {} }, { id: 'review', type: 'agent' as const, label: '审核', config: {} }, { id: 'explain', type: 'agent' as const, label: '说明', config: {} }],
      edges: [{ source: 'start', target: 'review', label: '输入' }, { source: 'review', target: 'explain', label: '结果' }],
    };
    expect(workflowOutputReferenceBinding(graph, 'explain', 'review', 'decision')).toEqual({ key: 'decision', source: 'node.review.output.decision' });
    expect(workflowOutputReferenceBinding(graph, 'explain', 'review', 'decision', [{ key: 'decision', source: 'input.topic' }])).toEqual({ key: 'decision_2', source: 'node.review.output.decision' });
    expect(workflowOutputReferenceBinding(graph, 'review', 'explain', 'decision')).toBeNull();
    expect(workflowOutputReferenceBinding(graph, 'explain', 'review', '字段')).toBeNull();
  });

  it('resolves run input and structured output references for trace previews', () => {
    const run = { output: { input: { topic: '合同' }, node_outputs: { review: '{"decision":"approve"}' }, agent_structured_outputs: { review: { decision: 'approve', score: 0.92 } } } } as never;
    expect(workflowRunInput(run)).toEqual({ topic: '合同' });
    expect(workflowRunReferenceValue(run, 'input.topic')).toBe('合同');
    expect(workflowRunReferenceValue(run, 'node.review.output.decision')).toBe('approve');
    expect(workflowRunReferenceValue(run, 'node.review.output.score')).toBe(0.92);
    expect(workflowValuePreview({ decision: 'approve' })).toBe('{"decision":"approve"}');
    expect(workflowValuePreview('secret-token', 'input.api_token')).toBe('[已隐藏]');
  });

  it('compares a node rerun while masking sensitive input changes', () => {
    const before = { output: { input: { topic: '合同', api_token: 'old-secret' }, agent_structured_outputs: { review: { decision: 'reject', score: 0.4 } } }, trace: [{ node_id: 'review', status: 'completed', role: '旧审核员', model_label: '旧模型', model_attempts: 1, model_retries: 0, duration_ms: 120, output_preview: '旧结论' }] } as never;
    const after = { output: { input: { topic: '制度', api_token: 'new-secret' }, agent_structured_outputs: { review: { decision: 'approve', score: 0.92 } } }, trace: [{ node_id: 'review', status: 'completed', role: '新审核员', model_label: '新模型', model_attempts: 2, model_retries: 1, duration_ms: 180, output_preview: '新结论' }] } as never;
    expect(workflowRunNodeComparison(before, after, 'review')).toMatchObject({
      input_changes: [{ key: 'api_token', before: '[已隐藏]', after: '[已隐藏]' }, { key: 'topic', before: '合同', after: '制度' }],
      structured_changes: [{ key: 'decision', before: 'reject', after: 'approve' }, { key: 'score', before: '0.4', after: '0.92' }],
      output_before: '旧结论', output_after: '新结论', output_changed: true,
      duration_delta_ms: 60, attempts_delta: 1, retries_delta: 1,
      model_before: '旧模型', model_after: '新模型', role_before: '旧审核员', role_after: '新审核员',
    });
    expect(workflowRunNodeComparison(before, after, 'missing')).toBeNull();
  });

  it('summarizes configuration changes between a historical version and the draft', () => {
    const historical = { nodes: [{ id: 'review', type: 'agent' as const, label: '审核', config: { mode: 'model', model_id: 'old-model', role: '审核员', system_prompt: '只看合同', arguments_json: '{"token":"old-secret"}' } }], edges: [] };
    const current = { nodes: [{ id: 'review', type: 'agent' as const, label: '审核', config: { mode: 'model', model_id: 'new-model', role: '复核员', system_prompt: '只看合同和制度', arguments_json: '{"token":"new-secret"}' } }], edges: [] };
    expect(workflowNodeConfigComparison(current, historical, 'review')?.changes).toEqual([
      { key: 'model_id', before: 'old-model', after: 'new-model' },
      { key: 'role', before: '审核员', after: '复核员' },
      { key: 'system_prompt', before: '只看合同', after: '只看合同和制度' },
    ]);
    expect(workflowConfigKeyLabel('system_prompt')).toBe('系统提示词');
  });

  it('shows structured output type, required, description, and extra-field changes', () => {
    const historical = { id: 'review', type: 'agent' as const, label: '审核', config: { output_schema_json: serializeWorkflowOutputSchema([{ name: 'decision', type: 'text' as const, required: true, description: '旧审批结论' }], false) } };
    const current = { id: 'review', type: 'agent' as const, label: '审核', config: { output_schema_json: serializeWorkflowOutputSchema([{ name: 'decision', type: 'boolean' as const, required: false, description: '新审批结论' }, { name: 'score', type: 'number' as const, required: true, description: '置信度' }], true) } };
    expect(workflowOutputSchemaComparison(current, historical)?.changes).toEqual([
      { key: 'decision', before: 'text · 必填 · 旧审批结论', after: 'boolean · 可选 · 新审批结论' },
      { key: 'score', before: '未声明', after: 'number · 必填 · 置信度' },
      { key: 'allow_extra', before: '不允许', after: '允许' },
    ]);
    expect(workflowConfigKeyLabel('allow_extra')).toBe('额外字段');
  });

  it('gives bounded recovery guidance for failed workflow nodes', () => {
    expect(workflowErrorRecoveryHint('WORKFLOW_AGENT_OUTPUT_INVALID')).toContain('输出字段');
    expect(workflowErrorRecoveryHint('WORKFLOW_MODEL_UNAVAILABLE')).toContain('授权');
    expect(workflowErrorRecoveryHint('UNKNOWN_WORKFLOW_ERROR')).toContain('节点配置');
  });

  it('inserts a new node between the selected step and its single successor', () => {
    const graph = {
      nodes: [{ id: 'start', type: 'input' as const, label: '开始', config: {} }, { id: 'plan', type: 'agent' as const, label: '计划', config: {} }, { id: 'review', type: 'approval' as const, label: '审批', config: {} }],
      edges: [{ source: 'start', target: 'plan', label: '输入' }, { source: 'plan', target: 'review', label: '提交审批' }],
    };
    const inserted = insertWorkflowNode(graph, { id: 'lookup', type: 'knowledge_search', label: '检索', config: {} }, 'plan');
    expect(inserted.edges).toEqual([{ source: 'start', target: 'plan', label: '输入' }, { source: 'plan', target: 'lookup', label: '下一步' }, { source: 'lookup', target: 'review', label: '提交审批' }]);
  });

  it('summarizes a run for the bounded history list', () => {
    const run = { id: 'run-1', workflow_id: 'flow-1', version: 3, graph_hash: 'hash', permission_digest: 'digest', status: 'awaiting_approval', synthetic: true, execution_mode: 'synthetic' as const, approval: { id: 'approval-1', workflow_run_id: 'run-1', workflow_id: 'flow-1', node_id: 'review', status: 'pending' as const, decision: null, comment: '', created_at: '2026-11-08T00:00:00Z', decided_at: null }, output: {}, trace: [{ sequence: 1, node_id: 'review', node_type: 'approval' as const, status: 'awaiting_approval', external_call: false, approval_required: true }], created_at: '2026-11-08T00:00:00Z', completed_at: null };
    expect(workflowRunSummary(run)).toMatchObject({ id: 'run-1', version: 3, trace_count: 1, approval_node_id: 'review' });
    expect(workflowRunHistoryLabel(workflowRunSummary(run))).toBe('等待审批');
  });

  it('validates manually configured skill input fields and artifact columns', () => {
    const skill = { id: 'form', type: 'skill' as const, label: '申请表', config: { form_fields_json: '[{\"id\":\"amount\",\"type\":\"number\",\"label\":\"金额\",\"required\":true}]' } };
    const artifact = { id: 'export', type: 'artifact' as const, label: '导出', config: { format: 'xlsx', columns: ['amount', 'date'] } };
    expect(workflowFormIssue({ nodes: [skill], edges: [] })).toBe('');
    expect(workflowArtifactIssue({ nodes: [artifact], edges: [] })).toBe('');
    expect(workflowFormIssue({ nodes: [{ ...skill, config: { form_fields_json: '[{\"id\":\"amount\",\"type\":\"choice\",\"label\":\"金额\"}]' } }], edges: [] })).toContain('选择字段');
    expect(workflowArtifactIssue({ nodes: [{ ...artifact, config: { columns: [''] } }], edges: [] })).toContain('产物字段');
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

  it('validates knowledge queries that read structured agent fields', () => {
    const plan = { id: 'plan', type: 'agent' as const, label: '计划', config: { mode: 'model', output_schema_json: '{"fields":[{"name":"topic","type":"text","required":true}]}' } };
    const lookup = { id: 'lookup', type: 'knowledge_search' as const, label: '检索', config: { query: '请检索 {{node.plan.output.topic}} 的规则' } };
    const graph = { nodes: [plan, lookup], edges: [{ source: 'plan', target: 'lookup', label: '依据' }] };
    expect(workflowKnowledgeIssue(graph)).toBe('');
    expect(workflowKnowledgeIssue({ ...graph, nodes: [plan, { ...lookup, config: { query: '{{node.plan.output.missing}}' } }] })).toContain('检索字段未在来源节点中声明');
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

  it('validates tool templates that read structured agent fields', () => {
    const review = { id: 'review', type: 'agent' as const, label: '审核', config: { mode: 'model', output_schema_json: '{"fields":[{"name":"decision","type":"text","required":true}]}' } };
    const read = { id: 'read', type: 'mcp_read' as const, label: '读取', config: { arguments_json: '{"query":"{{node.review.output.decision}}"}' } };
    const graph = { nodes: [review, read], edges: [{ source: 'review', target: 'read', label: '读取' }] };
    expect(workflowToolArgumentsIssue(graph)).toBe('');
    expect(workflowToolArgumentsIssue({ ...graph, nodes: [review, { ...read, config: { arguments_json: '{"query":"{{node.review.output.missing}}"}' } }] })).toContain('工具字段未在来源节点中声明');
    expect(workflowToolArgumentsIssue({ ...graph, nodes: [review, { ...read, config: { arguments_json: '{"query":"{{node.future.output.decision}}"}' } }] })).toContain('工具来源节点不存在');
  });
});
