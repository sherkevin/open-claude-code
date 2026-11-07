import { describe, expect, it } from 'vitest';
import { flowJSONToWorkflowGraph, workflowGraphHasCycle, workflowGraphToFlowJSON } from './workflowFlowgram';

describe('FlowGram workflow adapter', () => {
  it('keeps business nodes, edges and canvas positions across a round trip', () => {
    const graph = {
      nodes: [
        {
          id: 'agent-1',
          type: 'agent' as const,
          label: '拆解目标',
          config: { mode: 'synthetic', __ui: { position: { x: 128, y: 96 } } },
        },
        {
          id: 'approval-1',
          type: 'approval' as const,
          label: '人工确认',
          config: { __ui: { position: { x: 456, y: 96 } } },
        },
      ],
      edges: [{ source: 'agent-1', target: 'approval-1', label: '提交审批' }],
    };

    const flow = workflowGraphToFlowJSON(graph);
    const roundTrip = flowJSONToWorkflowGraph(flow);

    expect(roundTrip.nodes).toEqual(graph.nodes);
    expect(roundTrip.edges).toEqual(graph.edges);
  });

  it('uses stable defaults when a FlowGram document has incomplete metadata', () => {
    const graph = flowJSONToWorkflowGraph({
      nodes: [{ id: 'node-1', type: 'agent', data: { title: '未配置节点' } }],
      edges: [],
    });

    expect(graph.nodes[0]).toMatchObject({
      id: 'node-1',
      type: 'agent',
      label: '未配置节点',
      config: { mode: 'synthetic' },
    });
  });

  it('rejects a line that would make the graph cyclic', () => {
    const graph = {
      nodes: [
        { id: 'a', type: 'agent' as const, label: 'A', config: {} },
        { id: 'b', type: 'approval' as const, label: 'B', config: {} },
      ],
      edges: [{ source: 'a', target: 'b', label: '下一步' }],
    };
    expect(workflowGraphHasCycle(graph)).toBe(false);
    expect(workflowGraphHasCycle({ ...graph, edges: [...graph.edges, { source: 'b', target: 'a', label: '回流' }] })).toBe(true);
  });
});
