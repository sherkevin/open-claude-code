import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { SessionOverviewCards, SessionRowUsage, sessionOverviewDisplayMetrics } from './SessionObservability';
import { TrajectoryContent } from './SessionTrajectory';
import { appendTrajectory, durationText, filterTrajectory, metricDefinitions, metricValue, overviewMetrics, trajectoryDetails, trajectoryPath, trajectoryPositions, type SessionOverview, type SessionUsage, type TrajectoryItem } from './sessionObservationData';

const overview:SessionOverview={schema:'session-overview/v1',metrics:metricDefinitions.map(([id,title,unit])=>({id,title,unit,value:id==='session_count'?2:id==='cache_hit_rate'?.97:null,precision:id==='session_count'||id==='cache_hit_rate'?'observed':'unavailable',note:'未记录的值不按零统计。'})),sessions:[],coverage:{scope:'self',total_sessions:2,sessions_returned:2,sessions_truncated:false,total_runs:3,runs_included:3,runs_truncated:false,known_request_count:1,unknown_request_records:2,missing_usage_records:2,note:'只包含当前账号会话。'}};
const items:TrajectoryItem[]=[
  {id:'input-1',kind:'input',at:'2026-11-15T10:00:00Z',title:'用户输入',status:'received',text:'查询本周发货情况'},
  {id:'tool-1',kind:'tool',at:'2026-11-15T10:00:02Z',title:'查询发货',status:'succeeded',text:'已完成查询',details:{tool_name:'shipment_summary',authorization:'must-not-render',request_headers:'must-not-render',system_prompt:'must-not-render'}},
  {id:'model-1',kind:'model',at:'2026-11-15T10:00:05Z',title:'业务答复',status:'completed',text:'本周发货已汇总。'},
];

describe('session metric evidence boundaries',()=>{
  it('keeps preview off by default and isolates its ten fixed readings from actual receipts',()=>{
    const original=JSON.stringify(overview);
    const html=renderToStaticMarkup(<SessionOverviewCards value={overview} loading={false} error="" onRefresh={()=>{}}/>);
    expect(html).toContain('aria-pressed="false"');expect(html).toContain('预览效果');
    expect(sessionOverviewDisplayMetrics(overview)).toEqual(overviewMetrics(overview));
    const preview=sessionOverviewDisplayMetrics(overview,true);
    expect(preview).toHaveLength(10);
    expect(preview.every(metric=>metric.value!==null&&metric.precision==='estimated'&&metric.note.includes('仅用于界面预览，不计入实际用量'))).toBe(true);
    const values=Object.fromEntries(preview.map(metric=>[metric.id,metric.value!]));
    expect(values.cache_read_tokens/values.input_tokens).toBe(values.cache_hit_rate);
    expect(values.compression_saved_tokens-values.net_saved_tokens).toBe(6000);
    expect(sessionOverviewDisplayMetrics(overview,false)).toEqual(overviewMetrics(overview));
    expect(JSON.stringify(overview)).toBe(original);
  });
  it('keeps all ten requested metrics, ignores unexpected fields and retains the fixed display order',()=>{
    const values=overviewMetrics({...overview,metrics:[{id:'private_extra',title:'Private',unit:'count',value:999,precision:'observed' as const,note:'hidden'},...overview.metrics].reverse()});
    expect(values.map(v=>v.id)).toEqual(metricDefinitions.map(([id])=>id));
    expect(values).toHaveLength(10);
  });
  it('distinguishes missing or unavailable values from known zero and marks estimates',()=>{
    expect(metricValue(null,'tokens')).toBe('未记录');
    expect(metricValue(undefined,'USD')).toBe('未记录');
    expect(metricValue(NaN,'count')).toBe('未记录');
    expect(metricValue(Infinity,'count')).toBe('未记录');
    expect(metricValue(45,'count','unavailable')).toBe('未记录');
    expect(metricValue(0,'count','observed')).toBe('0');
    expect(metricValue(0,'USD','observed')).toBe('$0.00');
    expect(metricValue(.00003,'USD','estimated')).toBe('≈ $0.00003');
    expect(metricValue(.97,'ratio')).toBe('97%');
  });
  it('renders a self-scoped overview and never fills missing compression or costs with zero',()=>{
    const html=renderToStaticMarkup(<SessionOverviewCards value={overview} loading={false} error="" onRefresh={()=>{}}/>);
    expect(html.match(/data-metric=/g)).toHaveLength(10);
    expect(html).toContain('我的全部会话');
    expect(html).toContain('含已归档会话');
    expect(html).toContain('97%');
    expect(html).toContain('未记录');
    expect(html).not.toContain('$0.00');
    expect(html).toContain('计量口径与覆盖范围');
  });
  it('marks partial coverage and preserves its explanation',()=>{
    const value={...overview,metrics:overview.metrics.map(m=>m.id==='input_tokens'?{...m,value:1234,precision:'partial' as const,note:'另有 2 条记录缺少用量。'}:m)};
    const html=renderToStaticMarkup(<SessionOverviewCards value={value} loading={false} error="" onRefresh={()=>{}}/>);
    expect(html).toContain('1,234');expect(html).toContain('部分记录');expect(html).toContain('另有 2 条记录缺少用量。');
  });
  it('shows unavailable per-session readings instead of deriving them from visible message lengths',()=>{
    const html=renderToStaticMarkup(<SessionRowUsage/>);
    expect(html.match(/未记录/g)?.length).toBeGreaterThanOrEqual(6);
    expect(html).not.toContain('>0<');
  });
  it('uses complete input for the row and respects server labels for distinct measurement meanings',()=>{
    const html=renderToStaticMarkup(<SessionRowUsage value={{id:'one',input_tokens:10,total_input_tokens:100,cache_read_tokens:70,cache_write_tokens:20,token_precision:'observed'} as SessionUsage}/>);
    expect(html).toContain('<dt>输入 token</dt><dd title="100 token">100');
    const metric=overviewMetrics({...overview,metrics:overview.metrics.map(m=>m.id==='compression_saved_tokens'?{...m,title:'累计上下文减少'}:m)}).find(m=>m.id==='compression_saved_tokens');
    expect(metric?.title).toBe('累计上下文减少');
  });
  it('preserves individually known zero token counts when another input bucket is unavailable',()=>{
    const html=renderToStaticMarkup(<SessionRowUsage value={{id:'missing-input',total_input_tokens:null,cache_read_tokens:0,output_tokens:0,token_precision:'unavailable'} as SessionUsage}/>);
    expect(html).toContain('<dt>输入 token</dt><dd title="未记录">未记录');
    expect(html).toContain('<dt>缓存命中</dt><dd title="0 token">0');
    expect(html).toContain('<dt>输出 token</dt><dd title="0 token">0');
  });
});

describe('safe and paginated public trajectory',()=>{
  it('encodes both conversation and opaque pagination cursor',()=>{
    expect(trajectoryPath('id/other','a+b/secret?')).toBe('/conversations/id%2Fother/trajectory?limit=100&cursor=a%2Bb%2Fsecret%3F');
  });
  it('merges overlapping pages without duplicate events, retaining newer public event state',()=>{
    const updated={...items[1],status:'failed'};
    const merged=appendTrajectory(items.slice(0,2),[updated,items[2]]);
    expect(merged).toHaveLength(3);expect(merged[1].status).toBe('failed');expect(merged[0]).toEqual(items[0]);
  });
  it('searches only visible title/text/kind and combines the selected lane',()=>{
    expect(filterTrajectory(items,'查询','tool')).toEqual([items[1]]);
    expect(filterTrajectory(items,'must-not-render','all')).toEqual([]);
    expect(filterTrajectory(items,' 模型 ','all')).toEqual([items[2]]);
  });
  it('limits expanded details to safe known fields and never serializes raw request data',()=>{
    expect(trajectoryDetails(items[1].details)).toEqual([{label:'工具',value:'shipment_summary'}]);
    expect(trajectoryDetails({model_id:{secret:'hidden'},input_tokens:NaN,output_tokens:0,tool_name:'x'.repeat(201)})).toEqual([{label:'输出 token',value:'0'}]);
  });
  it('places missing or repeated timestamps safely inside the timeline',()=>{
    const positions=trajectoryPositions([...items,{...items[2],id:'invalid',at:'bad-time'}]);
    expect(positions.every(v=>Number.isFinite(v.position)&&v.position>=3&&v.position<=97)).toBe(true);
    expect(trajectoryPositions([])).toEqual([]);
    expect(durationText(null,null)).toBe('未记录');
    expect(durationText('2026-11-15T10:01:00Z','2026-11-15T10:00:00Z')).toBe('未记录');
    expect(durationText('2026-11-15T10:00:00Z','2026-11-15T10:01:05Z')).toBe('1 分 5 秒');
  });
  it('renders the three public lanes and explicit partial-page search scope without unsafe markup',()=>{
    const value=[...items,{id:'unsafe',kind:'input' as const,at:null,title:'<img src=x onerror=alert(1)>',status:'received',text:'<script>private()</script>'}];
    const html=renderToStaticMarkup(<TrajectoryContent page={{schema:'session-trajectory/v1',conversation_id:'own',items:value,next_cursor:'next',has_more:true}} items={value} loading={false} error="" onMore={()=>{}} onRefresh={()=>{}}/>);
    expect(html).toContain('输入、模型、工具三泳道');expect(html).toContain('搜索已加载的内容轨迹');expect(html).toContain('加载更多记录');
    expect(html).not.toContain('<script>');expect(html).not.toContain('<img');expect(html).not.toContain('must-not-render');
    expect(html).toContain('&lt;script&gt;');expect(html).toContain('模型请求');expect(html).toContain('未记录');
  });
});
