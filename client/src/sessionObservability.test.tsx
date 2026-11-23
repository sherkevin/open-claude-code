import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { SessionOverviewCards, SessionRowUsage } from './SessionObservability';
import { TrajectoryContent } from './SessionTrajectory';
import { appendTrajectory, durationText, filterTrajectory, metricValue, overviewMetrics, trajectoryDetails, trajectoryPath, trajectoryPositions, type OverviewMetric, type SessionOverview, type SessionUsage, type TrajectoryItem } from './sessionObservationData';

const overview: SessionOverview = {
  schema: 'session-observability/v1',
  metrics: [],
  sessions: [],
  coverage: {
    scope: 'self', total_sessions: 2, sessions_returned: 2, sessions_truncated: false,
    total_runs: 3, runs_included: 3, runs_truncated: false, known_request_count: 1,
    unknown_request_records: 2, missing_usage_records: 2, note: '只包含当前账号会话。',
  },
};
const completeMetrics = overviewMetrics(overview).map(metric => ({
  ...metric, value: metric.id === 'cache_hit_rate' ? 0.25 : 12,
  precision: metric.id === 'cost_usd' ? 'estimated' as const : 'observed' as const,
}));
function renderOverview(metrics: OverviewMetric[], loading = false, error = '') {
  return renderToStaticMarkup(<SessionOverviewCards value={{...overview, metrics}} loading={loading} error={error} onRefresh={()=>{}}/>);
}
function card(html: string, id: string) {
  return html.match(new RegExp(`<article[^>]+data-metric="${id}"[^>]*>.*?</article>`))?.[0];
}

const items:TrajectoryItem[]=[
  {id:'input-1',kind:'input',at:'2026-11-15T10:00:00Z',title:'用户输入',status:'received',text:'查询本周发货情况'},
  {id:'tool-1',kind:'tool',at:'2026-11-15T10:00:02Z',title:'查询发货',status:'succeeded',text:'已完成查询',details:{tool_name:'shipment_summary',authorization:'must-not-render',request_headers:'must-not-render',system_prompt:'must-not-render'}},
  {id:'model-1',kind:'model',at:'2026-11-15T10:00:05Z',title:'业务答复',status:'completed',text:'本周发货已汇总。'},
];

describe('session metric evidence boundaries', () => {
  it('keeps historical model events distinct from HTTP requests', () => {
    const historical: OverviewMetric = { id:'legacy_model_records', title:'旧版模型执行记录', value:3, unit:'条',
      precision:'observed', note:'网络重试明细未记录。' };
    const html=renderToStaticMarkup(<SessionOverviewCards value={{...overview, metrics:[historical]}}
      loading={false} error="" onRefresh={()=>{}}/>);
    expect(card(html, 'legacy_model_records')).toContain('>3</strong>');
    expect(html).not.toContain('data-metric="request_count"');
  });

  it('shows the exact recorded token subtotal on the overview card', () => {
    const input: OverviewMetric = { id: 'input_tokens', title: '输入 Token', unit: 'tokens', value: null,
      known_value: 1341601, precision: 'partial', note: '部分请求有回执。' };
    const html = renderToStaticMarkup(<SessionOverviewCards value={{...overview, metrics: [input]}}
      loading={false} error="" onRefresh={() => {}}/>);
    expect(card(html, 'input_tokens')).toContain('>1,341,601</strong>');
    expect(card(html, 'input_tokens')).toContain('已记录小计');
  });

  it('renders the recorded metrics with the core usage first and preserves their explanations', () => {
    const value = completeMetrics.map(metric => metric.id === 'input_tokens' ?
      {...metric, title: '输入 Token', note: '供应商报告的总输入。', known_value: 3} : metric);
    const html = renderOverview(value);
    expect([...html.matchAll(/data-metric="([^"]+)"/g)].map(match => match[1])).toEqual([
      'request_count', 'input_tokens', 'cache_read_tokens', 'output_tokens', 'compression_count',
      'session_count', 'cache_hit_rate', 'compression_saved_tokens', 'net_saved_tokens', 'compression_return_usd', 'cost_usd',
    ]);
    expect(card(html, 'input_tokens')).toContain('<strong title="12 tokens">12</strong>');
    expect(card(html, 'input_tokens')).toContain('输入 Token');
    expect(card(html, 'input_tokens')).toContain('供应商报告的总输入。');
    expect(card(html, 'cost_usd')).toContain('≈ $12.00');
    expect(card(html, 'cost_usd')).toContain('估算');
    expect(card(html, 'cache_hit_rate')).toContain('25%');
    expect(html).toContain('<summary>指标说明</summary>');
    expect(html).toContain('<dd>供应商报告的总输入。</dd>');
    expect(html).toContain('只包含当前账号会话。');
    expect(html).toContain('我的全部会话 · 含已归档会话');
    expect(html).toContain('aria-label="刷新会话概览"');
  });

  it('uses recorded subtotals without changing full values and qualifies partial amounts and rates', () => {
    const partialValues: Record<string, number> = {request_count: 3, input_tokens: 0, cost_usd: 0.07, cache_hit_rate: 0.97};
    const metrics = overviewMetrics(overview).map(metric => metric.id in partialValues ? {
      ...metric, title: metric.id === 'cache_hit_rate' ? '全局缓存命中率' : metric.title,
      precision: 'partial' as const, known_value: partialValues[metric.id], known_estimated: metric.id === 'cost_usd',
    } : metric);
    const html = renderOverview(metrics);
    expect(card(html, 'request_count')).toContain('<strong title="3 count">3</strong>');
    expect(card(html, 'request_count')).toContain('已记录小计');
    expect(card(html, 'request_count')).not.toContain('请求总数');
    expect(card(html, 'input_tokens')).toContain('<strong title="0 tokens">0</strong>');
    expect(card(html, 'input_tokens')).toContain('已记录小计');
    expect(card(html, 'cache_hit_rate')).toContain('97%');
    expect(card(html, 'cache_hit_rate')).toContain('按已记录用量计算');
    expect(card(html, 'cache_hit_rate')).not.toContain('全局');
    expect(card(html, 'cache_hit_rate')).not.toContain('小计');
    expect(card(html, 'cost_usd')).toContain('≈ $0.07');
    expect(card(html, 'cost_usd')).toContain('已记录金额');
    expect(card(html, 'cost_usd')).toContain('估算');
    expect(card(html, 'cost_usd')).not.toContain('小计');
    expect(card(html, 'compression_return_usd')).toBeUndefined();
    expect(html).toMatch(/<details[^>]*>.*压缩回本.*未记录/);
    expect(overviewMetrics({...overview, metrics})[0]).toMatchObject({value: null, known_value: 3});
  });

  it('keeps an older partial numeric reading and ignores an unknown metric', () => {
    const metric = {...completeMetrics[0], value: 3, precision: 'partial' as const};
    const privateMetric = {...metric, id: 'private_extra', title: 'Private', value: 999};
    const html = renderOverview([privateMetric, metric]);
    expect(card(html, 'request_count')).toContain('3</strong>');
    expect(card(html, 'request_count')).toContain('已记录小计');
    expect(html).not.toContain('Private');
  });

  it('shows an empty state with missing metrics and their reasons in the disclosure', () => {
    const html = renderOverview(overviewMetrics(overview).map(metric => metric.id === 'cost_usd' ? {
      ...metric, precision: 'partial', known_value: null, note: '供应商未返回费用。',
    } : metric));
    expect(html).toContain('暂无会话用量记录。');
    expect(html).not.toContain('data-metric=');
    expect(html).not.toContain('session-metric-grid');
    expect(html).toMatch(/<details[^>]*>.*花费（美元）.*未记录.*供应商未返回费用。/);
  });

  it('shows complete zero counts, zero USD and zero ratios as readings', () => {
    const html = renderOverview(completeMetrics.map(metric => ({...metric, value: 0, precision: 'observed'})));
    expect(card(html, 'compression_count')).toContain('<strong title="0 count">0</strong>');
    expect(card(html, 'cost_usd')).toContain('$0.00');
    expect(card(html, 'cache_hit_rate')).toContain('0%');
    expect(html).not.toContain('暂无会话用量记录');
    expect(html).not.toContain('未记录</small>');
  });

  it('uses a dedicated loading status and keeps stale values out of the overview', () => {
    const html = renderOverview(completeMetrics, true);
    expect(html).toContain('role="status"');
    expect(html).toContain('正在加载会话概览');
    expect(html).toMatch(/<button[^>]*aria-label="刷新会话概览"[^>]*disabled/);
    expect(html).not.toContain('role="alert"');
    expect(html).not.toContain('data-metric=');
    expect(html).not.toContain('暂无会话用量记录');
    expect(html).not.toContain('指标说明');
  });

  it('shows the fetch error as an alert with refresh available and hides stale readings', () => {
    const html = renderOverview(completeMetrics, false, '会话概览读取失败');
    expect(html).toMatch(/role="alert".*会话概览读取失败/);
    expect(html).toContain('aria-label="刷新会话概览"');
    expect(html).not.toMatch(/<button[^>]*disabled/);
    expect(html).not.toContain('data-metric=');
    expect(html).not.toContain('正在加载');
    expect(html).not.toContain('暂无会话用量记录');
  });

  it('distinguishes invalid or unavailable readings from zero when formatting values', () => {
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

  it('uses a compact empty state for a session without usage records', () => {
    const html = renderToStaticMarkup(<SessionRowUsage/>);
    expect(html).toContain('暂无用量记录。');
    expect(html).not.toContain('<dd');
    expect(html).not.toContain('>0<');
  });

  it('keeps legacy complete values and individual zeros even when the token group is incomplete', () => {
    const html = renderToStaticMarkup(<SessionRowUsage value={{
      id: 'one', input_tokens: 10, total_input_tokens: 100,
      cache_read_tokens: 0, output_tokens: 0, token_precision: 'unavailable',
    } as SessionUsage}/>);
    expect(html).toContain('<dt>输入 token</dt><dd title="100 token">100');
    expect(html).toContain('<dt>缓存命中</dt><dd title="0 token">0');
    expect(html).toContain('<dt>输出 token</dt><dd title="0 token">0');
    expect(html).not.toContain('已记录小计');
  });

  it('uses the matching session API fields for partial readings and prefers complete fields', () => {
    const html = renderToStaticMarkup(<SessionRowUsage value={{
      id: 'partial', request_count: null, request_precision: 'partial', input_tokens: 850,
      total_input_tokens: null, cache_read_tokens: null, output_tokens: 2,
      compression_count: 0, compression_saved_tokens: null,
      known_values: {request_count: 0, input_tokens: 600, cache_read_tokens: 34, output_tokens: 99},
    } as SessionUsage}/>);
    expect(html).toMatch(/<dt>请求<\/dt><dd title="0">0<small[^>]*>已记录小计/);
    expect(html).toMatch(/<dt>缓存命中<\/dt><dd title="34 token">34<small[^>]*>已记录小计/);
    expect(html).toContain('<dt>输出 token</dt><dd title="2 token">2</dd>');
    expect(html).toContain('<dt>压缩次数</dt><dd title="0">0</dd>');
    expect(html).not.toContain('<dt>输入 token</dt>');
    expect(html).not.toContain('<dt>上下文减少</dt>');
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
