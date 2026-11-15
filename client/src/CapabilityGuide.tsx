import { BookOpen, ChevronDown, Database, MessageSquare, ShieldCheck } from 'lucide-react';
import { readOnlyConfirmationState, remoteReadOnlyLabel } from './ToolReadOnlyConfirmation';
import type { Assistant, AssistantBinding, Connection, KnowledgeBase, MCPTool } from './types';
import './capability-guide.css';

const object = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value);
const scalar = (value: unknown): value is string | number | boolean => typeof value === 'string' || typeof value === 'boolean' || (typeof value === 'number' && Number.isFinite(value));
const parameterNames: Record<string, string> = { region: '区域', organization: '组织', factory: '工厂', customer: '客户', customer_id: '客户编号', date: '业务日期', date_from: '开始日期', date_to: '结束日期', dataset_rows: '数据集记录数', limit: '返回条数', page: '页码', query: '查询内容', status: '状态' };
const typeNames: Record<string, string> = { string: '文本', number: '数值', integer: '整数', boolean: '是 / 否', array: '列表', object: '对象', null: '空值' };
export function toolParameters(tool: MCPTool) {
  const required = Array.isArray(tool.inputSchema.required) ? tool.inputSchema.required : [];
  const properties = object(tool.inputSchema.properties) ? tool.inputSchema.properties : {};
  return Object.entries(properties).map(([key, value]) => {
    const schema = object(value) ? value : {};
    const choices = Array.isArray(schema.enum) ? schema.enum.filter(scalar) : scalar(schema.const) ? [schema.const] : [];
    return { key, label: parameterNames[key] || (typeof schema.title === 'string' ? schema.title : key), required: required.includes(key),
      type: typeof schema.type === 'string' ? typeNames[schema.type] || schema.type : '见完整定义',
      description: typeof schema.description === 'string' ? schema.description : '', choices,
      defaultValue: scalar(schema.default) ? schema.default : undefined,
      minimum: typeof schema.minimum === 'number' ? schema.minimum : undefined, maximum: typeof schema.maximum === 'number' ? schema.maximum : undefined };
  });
}

/** A display aid, never a grant or a replacement for server-side schema validation. */
export function toolApprovalSummary(connection: Connection, tool: MCPTool, binding?: AssistantBinding) {
  const confirmed = binding && readOnlyConfirmationState(connection, tool, binding.read_only_confirmations?.[tool.name]) === 'current';
  return {
    readOnly: confirmed ? '当前定义已确认只读' : remoteReadOnlyLabel(tool),
    approval: !binding ? '审批方式由所选助手决定' : !binding.allowed_tools.includes(tool.name) ? '此助手未授权此工具' : binding.auto_approved_tools.includes(tool.name) ? '助手配置为免确认；执行仍受服务端权限限制' : '调用前需要审批',
  };
}

export function usableAssistantTools(assistant: Assistant, connections: Connection[]) {
  const found = new Set<string>();
  return assistant.connections.flatMap(binding => {
    const connection = connections.find(item => item.id === binding.connection_id);
    if (!connection || !connection.enabled || connection.status !== 'connected') return [];
    return connection.tools.flatMap(tool => {
      const id = `${connection.id}:${tool.name}`;
      if (!binding.allowed_tools.includes(tool.name) || found.has(id)) return [];
      found.add(id); return [{ connection, tool, binding }];
    });
  });
}

export type CapabilityExample = { id: string; title: string; question: string; detail: string };
export function toolExample(connection: Connection, tool: MCPTool, binding?: AssistantBinding): CapabilityExample {
  const parameters = toolParameters(tool);
  const description = tool.description.trim();
  const demo = /演示|示例|合成|测试数据/.test(`${connection.name} ${description}`);
  const constrained = !!binding?.argument_constraints?.[tool.name];
  const dangerous = tool.annotations?.readOnlyHint !== true || tool.annotations?.destructiveHint === true;
  const specialTest = /取消验收|超限验收|故意返回|故障注入/.test(description);
  const id = `${connection.id}:${tool.name}`;
  if (dangerous || specialTest || !description) return { id, title: description ? '先了解使用条件' : '了解这个工具', detail: tool.name,
    question: `请说明「${connection.name}」的「${tool.name}」工具用途、需要提供的信息和审批要求。先不要调用或执行该工具。` };
  const preset = (key: string) => {
    const param = parameters.find(item => item.key === key);
    if (!param || constrained) return undefined;
    // Only a declared default is used; enum choices are not customer selections.
    return param.defaultValue !== undefined && (!param.choices.length || param.choices.includes(param.defaultValue)) ? param.defaultValue : undefined;
  };
  const region = preset('region'); const rows = preset('dataset_rows');
  const dates = [...new Set(description.match(/\b\d{4}-\d{2}-\d{2}\b/g) || [])];
  const scope = [typeof rows === 'number' ? `${rows.toLocaleString('zh-CN')}条${demo ? '演示' : ''}数据` : '', dates.length === 1 ? dates[0] : dates.length === 2 ? `${dates[0]}至${dates[1]}` : '', region !== undefined ? `区域“${region}”` : ''].filter(Boolean).join('，');
  // Business wording requires an explicit description, never a suggestive tool name.
  const shipment = /发货/.test(description) && /客户/.test(description) && /数量|件/.test(description);
  const orders = /订单/.test(description) && /总额|金额/.test(description);
  if (!shipment && !orders && !/查询|检索|读取|搜索/.test(description)) return { id, title: '了解工具与数据', detail: tool.name,
    question: `请根据「${connection.name}」的「${tool.name}」工具说明，介绍适用数据、需要的条件和输出内容。先不要调用该工具。` };
  const title = shipment ? '按客户查看发货数量' : orders ? '查看订单金额汇总' : '按工具说明查询';
  const action = shipment ? '按客户查看发货数量和合计' : orders ? '查看订单金额汇总' : `按照工具说明使用「${tool.name}」查询`;
  return { id, title, detail: `${connection.name} · ${tool.name}`,
    question: `请使用「${connection.name}」${scope ? `，在${scope}范围内` : '，在已授权范围内'}${action}，并注明数据范围和来源。${constrained || parameters.some(p => p.required && p.defaultValue === undefined) ? '缺少必要条件时请先问我，不要自行推定。' : ''}` };
}

export function assistantExamples(assistant: Assistant, connections: Connection[], knowledge: KnowledgeBase[]) {
  const tools = usableAssistantTools(assistant, connections);
  const examples = tools.filter(({ tool }) => tool.annotations?.readOnlyHint === true && tool.annotations?.destructiveHint !== true && !/取消验收|超限验收|故障注入/.test(tool.description)).slice(0, 2).map(({ connection, tool, binding }) => toolExample(connection, tool, binding));
  const kb = knowledge.find(item => item.enabled && item.document_count > 0 && assistant.knowledge_base_ids?.includes(item.id));
  if (kb) examples.push({ id: `knowledge:${kb.id}`, title: '查阅已接入的资料', detail: `${kb.name} · ${kb.document_count} 份文档`, question: `请查阅「${kb.name}」，先列出当前能检索到的文档及其主题，并引用来源。没有查到的内容请明确说明。` });
  return examples;
}

export function ExampleQuestions({ examples, disabled = false, onChoose }: { examples: CapabilityExample[]; disabled?: boolean; onChoose: (question: string) => void }) {
  if (!examples.length) return null;
  return <div className="capability-examples"><div className="capability-section-heading"><h3>可以这样问</h3><span>点击填入新会话，确认后再发送</span></div><div className="capability-example-grid">{examples.map(example => <button type="button" key={example.id} disabled={disabled} className="capability-example" onClick={() => onChoose(example.question)}><span><MessageSquare size={16}/><strong>{example.title}</strong></span><small>{example.detail}</small><p>{example.question}</p></button>)}</div></div>;
}

export function ToolCapability({ connection, tool, binding, onChoose }: { connection: Connection; tool: MCPTool; binding?: AssistantBinding; onChoose?: (question: string) => void }) {
  const parameters = toolParameters(tool); const approval = toolApprovalSummary(connection, tool, binding);
  const metadataNames: Record<string, string> = { database: '数据库', table: '数据表', tables: '数据表', timezone: '时区', organization: '组织', region: '区域', factory: '工厂', time_range: '时间范围', data_scope: '数据范围', data_type: '数据类型' };
  const metadata = Object.entries(connection.source_metadata || {}).filter(([key]) => key in metadataNames);
  return <details className="capability-tool" open={connection.tools.length === 1}>
    <summary><div><strong>{tool.description?.split(/[。\n]/)[0] || '工具尚未提供用途说明'}</strong><code>{tool.name}</code></div><ChevronDown size={17}/></summary>
    <div className="capability-tool-body"><p className="capability-purpose">{tool.description || '请让 MCP 服务提供工具说明，或先向维护人员核实用途。'}</p>
      <div className="capability-access"><ShieldCheck size={16}/><span>{approval.readOnly}<small>{approval.approval}</small></span></div>
      <p className="capability-note">服务的只读提示不等于权限保证；免确认也不代表只读。</p>
      <h4>适用数据与范围</h4>{metadata.length ? <dl className="capability-scope">{metadata.map(([key, value]) => <div key={key}><dt>{metadataNames[key]}</dt><dd>{value}</dd></div>)}</dl> : <p className="capability-note">连接未单独提供数据范围。请结合下方工具参数选择条件，以每次返回的来源和范围为准。</p>}
      <h4>提问时可以指定</h4>{parameters.length ? <div className="capability-parameters">{parameters.map(param => <div key={param.key}><div><strong>{param.label}</strong><code>{param.key}</code><span>{param.required ? '必填' : '可选'} · {param.type}</span></div>{param.description && <p>{param.description}</p>}{!!param.choices.length && <p>可选值：{param.choices.map(String).join('、')}</p>}{param.defaultValue !== undefined && <p>工具默认值：{String(param.defaultValue)}</p>}{(param.minimum !== undefined || param.maximum !== undefined) && <p>{param.minimum !== undefined && `最小值 ${param.minimum}`}{param.minimum !== undefined && param.maximum !== undefined && ' · '}{param.maximum !== undefined && `最大值 ${param.maximum}`}</p>}</div>)}</div> : <p className="capability-note">工具未列出可展开的顶层参数，完整规则见下方定义。</p>}
      {binding?.argument_constraints?.[tool.name] && <details className="capability-technical"><summary>此助手的请求限制</summary><p>请求需同时满足工具定义和以下限制；这不代表返回数据范围已验证。</p><pre>{JSON.stringify(binding.argument_constraints[tool.name], null, 2)}</pre></details>}
      {onChoose && <ExampleQuestions examples={[toolExample(connection, tool, binding)]} onChoose={onChoose}/>}
      <details className="capability-technical"><summary>完整参数定义（JSON Schema）</summary><pre>{JSON.stringify(tool.inputSchema, null, 2)}</pre></details>
    </div>
  </details>;
}

export function AssistantCapabilityGuide({ assistant, connections, knowledge, disabled, onChoose }: { assistant: Assistant; connections: Connection[]; knowledge: KnowledgeBase[]; disabled?: boolean; onChoose: (question: string) => void }) {
  const tools = usableAssistantTools(assistant, connections);
  const boundKnowledge = knowledge.filter(item => assistant.knowledge_base_ids?.includes(item.id));
  const readyKnowledge = boundKnowledge.filter(item => item.enabled && item.document_count > 0);
  const examples = assistantExamples(assistant, connections, knowledge);
  return <section className="assistant-capability-guide" aria-label="助手用途与提问示例"><div className="capability-section-heading"><h2>这个助手能做什么</h2><span>根据已保存的配置</span></div>
    <div className="capability-overview"><div><Database size={19}/><strong>{tools.length} 项业务工具</strong><p>{tools.length ? '查看已授权工具的用途、条件与审批方式。' : '暂无已接通且授权的业务工具。'}</p></div><div><BookOpen size={19}/><strong>{readyKnowledge.length} 个已绑定知识库</strong><p>{readyKnowledge.length ? readyKnowledge.map(item => `${item.name}（${item.document_count}份）`).join('、') : '尚未绑定包含文档的启用知识库。'}</p></div></div>
    <ExampleQuestions examples={examples} disabled={disabled || !assistant.enabled} onChoose={onChoose}/>
    {!examples.length && <p className="capability-note">接入资料或可查询工具后，这里会显示对应的提问示例。你也可以新建会话，说明需要完成的工作。</p>}
    {tools.length > 0 && <details className="capability-tool-list"><summary>查看业务能力与适用条件 · {tools.length} 项<ChevronDown size={16}/></summary>{tools.map(({ connection, tool, binding }) => <ToolCapability key={`${connection.id}:${tool.name}`} connection={connection} tool={tool} binding={binding} onChoose={!disabled && assistant.enabled ? onChoose : undefined}/>)}</details>}
    {assistant.connections.length > 0 && tools.length === 0 && <p className="capability-note">已保存连接绑定，但当前未发现可用的已授权工具，请核对连接状态与工具选择。</p>}
    {boundKnowledge.some(item => !item.enabled || item.document_count === 0) && <p className="capability-note">部分已绑定知识库处于停用或无文档状态，未列为可提问资料。</p>}
  </section>;
}
