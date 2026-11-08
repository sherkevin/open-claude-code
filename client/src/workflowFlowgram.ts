import type { WorkflowJSON, WorkflowNodeJSON } from '@flowgram.ai/free-layout-editor';
import type { WorkflowGraph, WorkflowNodeType } from './types';

const WORKFLOW_NODE_TYPES: WorkflowNodeType[] = [
  'input',
  'agent',
  'skill',
  'knowledge_search',
  'mcp_read',
  'condition',
  'approval',
  'artifact',
];

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === 'object' && !Array.isArray(value);
}

function workflowNodeType(value: unknown): WorkflowNodeType {
  return typeof value === 'string' && WORKFLOW_NODE_TYPES.includes(value as WorkflowNodeType)
    ? (value as WorkflowNodeType)
    : 'agent';
}

function positionFor(index: number): { x: number; y: number } {
  return {
    // Keep the initial graph readable in the split canvas + inspector layout.
    // Users can still drag nodes freely and the deliberate position is stored
    // in __ui after the first canvas edit.
    x: 48 + (index % 3) * 230,
    y: 48 + Math.floor(index / 3) * 160,
  };
}

function uiPosition(config: Record<string, unknown>): { x: number; y: number } | undefined {
  const ui = isRecord(config.__ui) ? config.__ui : undefined;
  const position = ui && isRecord(ui.position) ? ui.position : undefined;
  if (typeof position?.x !== 'number' || typeof position.y !== 'number') return undefined;
  return { x: position.x, y: position.y };
}

function withUiPosition(config: Record<string, unknown>, position: unknown): Record<string, unknown> {
  if (!isRecord(position) || typeof position.x !== 'number' || typeof position.y !== 'number') {
    return { ...config };
  }
  const currentUi = isRecord(config.__ui) ? config.__ui : {};
  return { ...config, __ui: { ...currentUi, position: { x: position.x, y: position.y } } };
}

/** Convert the server graph into the JSON document consumed by FlowGram. */
export function workflowGraphToFlowJSON(graph: WorkflowGraph): WorkflowJSON {
  return {
    nodes: graph.nodes.map((node, index) => ({
      id: node.id,
      type: node.type,
      meta: { position: uiPosition(node.config) || positionFor(index) },
      data: {
        title: node.label,
        nodeType: node.type,
        config: node.config,
      },
    })),
    edges: graph.edges.map(edge => ({
      sourceNodeID: edge.source,
      targetNodeID: edge.target,
      data: { label: edge.label },
    })),
  };
}

function nodeConfig(node: WorkflowNodeJSON): Record<string, unknown> {
  return isRecord(node.data?.config) ? { ...node.data.config } : { mode: 'synthetic' };
}

/** Convert FlowGram edits back into the existing server graph contract. */
export function flowJSONToWorkflowGraph(json: WorkflowJSON): WorkflowGraph {
  return {
    nodes: json.nodes.map(node => {
      const config = withUiPosition(nodeConfig(node), node.meta?.position);
      const data = isRecord(node.data) ? node.data : {};
      return {
        id: String(node.id),
        type: workflowNodeType(node.type),
        label: typeof data.title === 'string' ? data.title : String(node.id),
        config,
      };
    }),
    edges: json.edges.map(edge => ({
      source: String(edge.sourceNodeID),
      target: String(edge.targetNodeID),
      label: isRecord(edge.data) && typeof edge.data.label === 'string' ? edge.data.label : '下一步',
    })),
  };
}

/** Keep client-side line validation aligned with the server graph contract. */
export function workflowGraphHasCycle(graph: WorkflowGraph): boolean {
  const indegree = new Map(graph.nodes.map(node => [node.id, 0]));
  const next = new Map<string, string[]>();
  graph.nodes.forEach(node => next.set(node.id, []));

  graph.edges.forEach(edge => {
    if (!indegree.has(edge.target) || !next.has(edge.source)) return;
    indegree.set(edge.target, (indegree.get(edge.target) || 0) + 1);
    next.get(edge.source)?.push(edge.target);
  });

  const queue = [...indegree.entries()].filter(([, value]) => value === 0).map(([id]) => id);
  let visited = 0;
  while (queue.length) {
    const id = queue.shift()!;
    visited += 1;
    for (const target of next.get(id) || []) {
      const value = (indegree.get(target) || 0) - 1;
      indegree.set(target, value);
      if (value === 0) queue.push(target);
    }
  }
  return visited !== graph.nodes.length;
}
