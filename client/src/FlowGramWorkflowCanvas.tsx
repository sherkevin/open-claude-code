import { useEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import {
  EditorRenderer,
  Field,
  FreeLayoutEditorProvider,
  WorkflowNodeRenderer,
  useClientContext,
  useNodeRender,
  usePlaygroundTools,
  type FlowNodeMeta,
  type FreeLayoutProps,
  type WorkflowNodeProps,
  type WorkflowNodeRegistry,
} from '@flowgram.ai/free-layout-editor';
import '@flowgram.ai/free-layout-editor/index.css';
import { Redo2, Undo2, ZoomIn, ZoomOut, Maximize2 } from 'lucide-react';
import type { WorkflowGraph, WorkflowNodeType } from './types';
import { flowJSONToWorkflowGraph, workflowGraphHasCycle, workflowGraphToFlowJSON } from './workflowFlowgram';

const NODE_TYPES: WorkflowNodeType[] = [
  'agent',
  'skill',
  'knowledge_search',
  'mcp_read',
  'condition',
  'approval',
  'artifact',
];

const NODE_LABELS: Record<WorkflowNodeType, string> = {
  agent: '智能体节点',
  skill: '工作方法',
  knowledge_search: '知识检索',
  mcp_read: '只读工具',
  condition: '条件分支',
  approval: '人工审批',
  artifact: '产物输出',
};

const NODE_REGISTRIES: WorkflowNodeRegistry[] = NODE_TYPES.map(type => ({
  type,
  meta: {
    defaultPorts: [{ type: 'input' }, { type: 'output' }],
  },
}));

function DefaultNodeForm({ editable }: { editable: boolean }) {
  return (
    <div className="zhixing-flow-node-form">
      <Field<string> name="title">
        {({ field }) => (
          <input
            className="zhixing-flow-node-title-input"
            aria-label="画布节点名称"
            disabled={!editable}
            value={field.value || ''}
            onChange={event => field.onChange(event.target.value)}
          />
        )}
      </Field>
      <Field<string> name="nodeType">
        {({ field }) => <span className="zhixing-flow-node-type">{field.value || '业务节点'}</span>}
      </Field>
      <Field<Record<string, unknown>> name="config">
        {({ field }) => {
          const config = field.value || {};
          const mode = config.mode === 'model' ? '模型执行' : '合成执行';
          const model = typeof config.model_id === 'string' && config.model_id ? ` · ${config.model_id}` : '';
          return <span className={`zhixing-flow-node-mode ${config.mode === 'model' ? 'zhixing-flow-node-model' : ''}`}>{mode}{model} · 可审计</span>;
        }}
      </Field>
    </div>
  );
}

function FlowNode({
  props,
  editable,
  onSelectNode,
}: {
  props: WorkflowNodeProps;
  editable: boolean;
  onSelectNode: (nodeId: string) => void;
}) {
  const { node, form, selected, activated } = useNodeRender();
  const type = (typeof node.flowNodeType === 'string' ? node.flowNodeType : 'agent') as WorkflowNodeType;
  const meta = node.getNodeMeta<FlowNodeMeta>();

  useEffect(() => {
    if (selected || activated) onSelectNode(String(node.id));
  }, [activated, node.id, onSelectNode, selected]);

  return (
    <WorkflowNodeRenderer
      node={props.node}
      className={`zhixing-flow-node zhixing-flow-node-${type.replaceAll('_', '-')}`}
      style={meta.wrapperStyle}
    >
      <div className="zhixing-flow-node-head">
        <span className="zhixing-flow-node-dot" />
        <span>{NODE_LABELS[type] || '业务节点'}</span>
        <span className="zhixing-flow-node-number">{String(node.id).slice(0, 6)}</span>
      </div>
      {form?.render() || <DefaultNodeForm editable={editable} />}
    </WorkflowNodeRenderer>
  );
}

function FlowGramToolbar({ editable }: { editable: boolean }) {
  const tools = usePlaygroundTools();
  const { history } = useClientContext();
  const [canUndo, setCanUndo] = useState(history.canUndo());
  const [canRedo, setCanRedo] = useState(history.canRedo());

  useEffect(() => {
    const disposable = history.undoRedoService.onChange(() => {
      setCanUndo(history.canUndo());
      setCanRedo(history.canRedo());
    });
    return () => disposable.dispose();
  }, [history]);

  return (
    <div className="zhixing-flowgram-toolbar" aria-label="画布工具">
      <span className="zhixing-flowgram-toolbar-label">画布</span>
      <button type="button" aria-label="缩小画布" onClick={() => tools.zoomout()}>
        <ZoomOut size={14} />
      </button>
      <span className="zhixing-flowgram-zoom">{Math.round(tools.zoom * 100)}%</span>
      <button type="button" aria-label="放大画布" onClick={() => tools.zoomin()}>
        <ZoomIn size={14} />
      </button>
      <button type="button" aria-label="适应画布" onClick={() => tools.fitView()}>
        <Maximize2 size={14} />
      </button>
      <span className="zhixing-flowgram-toolbar-divider" />
      <button type="button" aria-label="撤销" disabled={!editable || !canUndo} onClick={() => history.undo()}>
        <Undo2 size={14} />
      </button>
      <button type="button" aria-label="重做" disabled={!editable || !canRedo} onClick={() => history.redo()}>
        <Redo2 size={14} />
      </button>
    </div>
  );
}

export default function FlowGramWorkflowCanvas({
  graph,
  editable,
  resetToken,
  onChange,
  onSelectNode,
}: {
  graph: WorkflowGraph;
  editable: boolean;
  resetToken: string | number;
  onChange: (graph: WorkflowGraph) => void;
  onSelectNode: (nodeId: string) => void;
}) {
  const readyRef = useRef(false);
  const graphRef = useRef(graph);
  const editableRef = useRef(editable);
  graphRef.current = graph;
  editableRef.current = editable;
  const initialData = useMemo(() => workflowGraphToFlowJSON(graph), [graph, resetToken]);
  const editorProps = useMemo<FreeLayoutProps>(
    () => ({
      background: true,
      readonly: !editable,
      initialData,
      nodeRegistries: NODE_REGISTRIES,
      getNodeDefaultRegistry: type => ({
        type,
        meta: {
          defaultExpanded: true,
          defaultPorts: [{ type: 'input' }, { type: 'output' }],
        },
        formMeta: {
          render: () => <DefaultNodeForm editable={editable} />,
        },
      }),
      materials: {
        renderDefaultNode: (props: WorkflowNodeProps) => (
          <FlowNode props={props} editable={editable} onSelectNode={onSelectNode} />
        ),
      },
      onContentChange: ctx => {
        if (!readyRef.current) return;
        onChange(flowJSONToWorkflowGraph(ctx.document.toJSON()));
      },
      onAllLayersRendered: ctx => {
        readyRef.current = true;
        ctx.document.fitView(false);
      },
      nodeEngine: { enable: true },
      history: { enable: true, enableChangeNode: true },
      canDeleteNode: () => editable,
      canDeleteLine: () => editable,
      canAddLine: (_ctx, fromPort, toPort) => {
        if (!editableRef.current || fromPort.portType !== 'output' || toPort.portType !== 'input') return false;
        const source = String(fromPort.node.id);
        const target = String(toPort.node.id);
        if (source === target || graphRef.current.edges.some(edge => edge.source === source && edge.target === target)) return false;
        return !workflowGraphHasCycle({
          ...graphRef.current,
          edges: [...graphRef.current.edges, { source, target, label: '下一步' }],
        });
      },
      lineColor: {
        hidden: 'transparent',
        default: '#a8bba0',
        drawing: '#7ca46e',
        hovered: '#7ca46e',
        selected: '#6d9560',
        error: '#be7b68',
        flowing: '#6d9560',
      },
    }),
    [editable, initialData, onChange, onSelectNode],
  );

  return (
    <div
      className="zhixing-flowgram-shell"
      style={{ '--zhixing-flow-reset': resetToken } as CSSProperties}
    >
      <FreeLayoutEditorProvider key={resetToken} {...editorProps}>
        <EditorRenderer className="zhixing-flowgram-editor" />
        <FlowGramToolbar editable={editable} />
      </FreeLayoutEditorProvider>
    </div>
  );
}
