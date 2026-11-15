import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { BusinessResultCard, objectPreview, parseBusinessResult, resultCSV } from './BusinessResult';
import { ConnectionGroups, ConnectionOnboardingCard } from './ConnectionsPage';
import WorkspacePage, { WorkspaceConnectionSummary, workspaceConnectionOverview, workspaceConnectionStatus } from './WorkspacePage';
import { usageCoverage, usageValue } from './AdminPages';
import type { Assistant, Connection, UsageTotals } from './types';

const connection: Connection={id:'orders',name:'订单',url:'https://example.test/mcp',transport:'http',auth_type:'none',credential_present:false,enabled:true,status:'connected',tools:[{name:'sum',description:'',inputSchema:{}}],last_tested_at:null,last_error:null,revision:1,can_edit:true};
const assistant:Assistant={id:'a',name:'业务助手',description:'查询授权范围',instructions:'',enabled:true,revision:1,access:{all_members:true,user_ids:[]},connections:[{connection_id:'orders',allowed_tools:['sum'],auto_approved_tools:[]}]};
const result=(overrides:Record<string,unknown>={})=>parseBusinessResult({schema_version:'business-result/v1',title:'客户发货',columns:[{key:'customer',label:'客户'},{key:'quantity',label:'数量',unit:'件'},{key:'amount',label:'金额',unit:'CNY'}],rows:[{customer:'甲厂',quantity:0,amount:null}],complete:false,total_rows:null,synthetic:true,warnings:[],...overrides})!;
const noop=()=>{};

describe('business object previews retain result boundaries',()=>{
  it('uses returned identity and values without inventing business links or totals',()=>{
    const data=result();const preview=objectPreview(data,data.rows[0],0);
    expect(preview.title).toBe('客户：甲厂');
    const html=renderToStaticMarkup(<BusinessResultCard result={data} runId="r" eventId={1}/>);
    expect(html).toContain('客户：甲厂');expect(html).toContain('>0</dd>');expect(html).toContain('>—</dd>');
    expect(html).toContain('部分结果');expect(html).not.toContain('打开原单据');expect(html).not.toContain('<a ');
    expect(html).toContain('aria-pressed="true"');expect(html).toContain('查询范围与依据');
  });
  it('renders six returned objects initially while CSV retains the exact full returned version',()=>{
    const data=result({rows:Array.from({length:8},(_,i)=>({customer:`客户-${i+1}`,quantity:i,amount:i*10}))});
    const html=renderToStaticMarkup(<BusinessResultCard result={data} runId="r" eventId={2}/>);
    expect(html).toContain('客户-6');expect(html).not.toContain('客户-7');expect(html).toContain('再显示 2 行');
    expect(resultCSV(data,'r',2)).toContain('客户-8');expect(html).toContain('部分结果');
  });
  it('keeps additional fields and scope available behind native expandable controls',()=>{
    const data=result({columns:[{key:'customer',label:'客户'},...['a','b','c','d'].map(key=>({key,label:key}))],rows:[{customer:'甲厂',a:1,b:2,c:3,d:'原始字段'}],scope:{organization:'授权组织',aggregation:'按客户',timezone:'Asia/Shanghai'},source_ids:['source_1']});
    const html=renderToStaticMarkup(<BusinessResultCard result={data} runId="r" eventId={2}/>);
    expect(html).toContain('<details class="br-field-details">');expect(html).toContain('原始字段');
    expect(html).toContain('<details class="br-evidence">');expect(html).toContain('按客户');expect(html).toContain('查看依据 1');
    expect(html).not.toContain('href="source_1"');
  });
  it('does not turn an empty result into a zero-value object',()=>{
    const html=renderToStaticMarkup(<BusinessResultCard result={result({rows:[]})} runId="r" eventId={2}/>);
    expect(html).toContain('本次查询未返回记录');expect(html).not.toContain('class="br-object"');
  });
});

describe('MCP resource grouping',()=>{
  it('keeps disabled resources manageable without adding assistant flows',()=>{
    const disabled={...connection,id:'off',name:'停用连接',enabled:false};
    const items=[disabled,{...connection,name:'当前 MCP'}];const original=items.slice();
    const renderCard=(item:Connection)=><ConnectionOnboardingCard key={item.id} item={item} admin={false} onTest={noop} onTools={noop} onEdit={noop} onDelete={noop}/>;
    const html=renderToStaticMarkup(<ConnectionGroups items={items} renderCard={renderCard}/>);
    expect(html.indexOf('当前 MCP')).toBeLessThan(html.indexOf('已停用（1）'));
    expect(html).toContain('<details class="connections-disabled">');
    expect(html).toContain('aria-label="编辑 停用连接"');expect(html).toContain('aria-label="删除 停用连接"');
    expect(html).toContain('工具列表');expect(html).not.toContain('配置助手');expect(html).not.toContain('开始查询');expect(items).toEqual(original);
    const onlyDisabled=renderToStaticMarkup(<ConnectionGroups items={[disabled]} renderCard={renderCard}/>);
    expect(onlyDisabled).toContain('暂无启用的 MCP');expect(onlyDisabled).toContain('已停用（1）');
  });
});
describe('workspace continues real work before showing capabilities',()=>{
  it('puts active failures first, bounds the active list, and folds disabled records without mutating them',()=>{
    const items=[...Array.from({length:5},(_,i)=>({...connection,id:`disabled-${i}`,name:`停用测试${i}`,enabled:false})),
      {...connection,id:'ready',name:'正式业务连接'}, {...connection,id:'auth',name:'需要认证',status:'needs_auth' as const},
      {...connection,id:'failed',name:'测试失败项',status:'failed' as const},
      {...connection,id:'ready2'}, {...connection,id:'untested',status:'untested' as const}];
    const original=items.slice();
    const overview=workspaceConnectionOverview(items);
    expect(overview.visible.map(item=>item.id)).toEqual(['auth','failed','ready']);
    expect(overview).toMatchObject({enabledCount:5,disabledCount:5});expect(items).toEqual(original);
    const html=renderToStaticMarkup(<WorkspaceConnectionSummary connections={items} onManage={noop}/>);
    expect(html).toContain('正式业务连接');expect(html).toContain('另有 5 个停用连接');expect(html).toContain('查看全部 5 个启用连接');
    expect(html).not.toContain('停用测试');
    expect(renderToStaticMarkup(<WorkspaceConnectionSummary connections={items.slice(0,5)} onManage={noop}/>)).toContain('暂无启用连接');
  });
  it('prioritizes actual assistant bindings without hiding failures or deleting similarly named connections',()=>{
    const items=[{...connection,id:'unbound',name:'订单演示'}, {...connection,id:'bound',name:'订单演示'},
      {...connection,id:'failed',status:'failed' as const}, {...connection,id:'bound-untested',status:'untested' as const}];
    expect(workspaceConnectionOverview(items,['bound','bound-untested']).visible.map(item=>item.id)).toEqual(['failed','bound','bound-untested']);
    expect(workspaceConnectionOverview(items,['bound']).enabledCount).toBe(4);
    expect(items.map(item=>item.id)).toEqual(['unbound','bound','failed','bound-untested']);
  });
  it('keeps the question entry and assistant start without duplicated page-header or empty-state creation buttons',()=>{
    const html=renderToStaticMarkup(<WorkspacePage assistants={[assistant]} conversations={[]} onAssistant={noop} onConversation={noop} onNavigate={noop} onNew={noop} onStart={noop}/>);
    expect(html).toContain('id="wr-question"');expect(html).toContain('开始会话');expect(html).not.toContain('新建会话');
    expect(html).toContain('从上方提问开始');
  });
  it('orders work before assistants and distinguishes connection observation states',()=>{
    const html=renderToStaticMarkup(<WorkspacePage assistants={[assistant]} conversations={[]} onAssistant={noop} onConversation={noop} onNavigate={noop} onNew={noop} onStart={noop}/>);
    expect(html.indexOf('id="wr-recent-heading"')).toBeLessThan(html.indexOf('id="wr-assistants-heading"'));
    expect(html).toContain('rows="1"');expect(html).not.toContain('连接与资料');
    expect(workspaceConnectionStatus(connection)).toBe('已发现 1 个工具');
    expect(workspaceConnectionStatus({...connection,enabled:false})).toBe('已停用');
    expect(workspaceConnectionStatus({...connection,tools:[]})).toBe('未发现工具');
  });
});

const usage:UsageTotals={total_runs:2,successful_runs:2,input_tokens:null,output_tokens:0,estimated_cost_usd:null,known_subtotals:{input_tokens:42,output_tokens:0,estimated_cost_usd:0},field_coverage:{input_tokens:{known_runs:1,unknown_runs:1},output_tokens:{known_runs:2,unknown_runs:0},estimated_cost_usd:{known_runs:0,unknown_runs:2}},reported_usage_runs:1,missing_usage_runs:1,usage_complete:false};
describe('usage summary never formats unknown as zero',()=>{
  it('separates incomplete totals, known partials and complete measured zero',()=>{
    expect(usageValue(usage,'input_tokens')).toBe('未完整报告');expect(usageValue(usage,'estimated_cost_usd')).toBe('未完整报告');
    expect(usageCoverage(usage,'input_tokens')).toBe('已知小计 42 · 1 / 2 个任务已报告');
    expect(usageValue(usage,'output_tokens')).toBe('0');
    expect(usageCoverage(usage,'estimated_cost_usd')).toBe('0 / 2 个任务已报告');
  });
  it('does not promote a contradictory legacy zero or NaN into complete totals',()=>{
    expect(usageValue({...usage,input_tokens:0},'input_tokens')).toBe('未完整报告');
    expect(usageValue({...usage,output_tokens:NaN},'output_tokens')).toBe('未完整报告');
    expect(usageCoverage({...usage,field_coverage:undefined},'input_tokens')).toBe('报告覆盖范围未提供');
  });
});
