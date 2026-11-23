export type MetricPrecision = 'observed' | 'estimated' | 'unavailable' | 'partial';
export type OverviewMetric = {
  id:string; title:string; value:number|null; unit:string; precision:MetricPrecision; note:string;
  known_value?:number|null; known_estimated?:boolean;
};
type SessionUsageField = 'legacy_model_records' | 'request_count' | 'input_tokens' | 'total_input_tokens' | 'output_tokens' |
  'cache_read_tokens' | 'cache_write_tokens' | 'cost_usd' | 'compression_count' |
  'compression_saved_tokens' | 'net_saved_tokens' | 'compression_return_usd';
export type SessionUsage = {
  id:string; title:string; updated_at:string; status:'running'|'idle'|'archived'; run_count:number;
  legacy_model_records?:number|null; request_count:number|null; input_tokens:number|null; total_input_tokens:number|null; output_tokens:number|null;
  cache_read_tokens:number|null; cache_write_tokens:number|null; cost_usd:number|null;
  compression_count:number|null; compression_saved_tokens:number|null; net_saved_tokens:number|null;
  compression_return_usd:number|null; token_precision:MetricPrecision; request_precision:MetricPrecision;
  cost_precision:MetricPrecision; compression_precision:MetricPrecision;
  known_values?:Partial<Record<SessionUsageField, number|null>>;
  known_estimated_fields?:SessionUsageField[];
};
export type SessionOverview = {
  schema:string; metrics:OverviewMetric[]; sessions:SessionUsage[];
  coverage:{scope:'self';total_sessions:number;sessions_returned:number;sessions_truncated:boolean;
    total_runs:number;runs_included:number;runs_truncated:boolean;known_request_count:number;
    unknown_request_records:number;missing_usage_records:number;note:string};
};
export const metricDefinitions = [
  ['request_count','模型请求总数','count'], ['input_tokens','输入 token','tokens'],
  ['cache_read_tokens','缓存命中 token','tokens'], ['output_tokens','输出 token','tokens'],
  ['compression_count','压缩次数','count'], ['session_count','会话总数','count'],
  ['cache_hit_rate','缓存命中率','ratio'], ['compression_saved_tokens','压缩上下文减少','tokens'],
  ['net_saved_tokens','净节省','tokens'], ['compression_return_usd','压缩回本','USD'],
  ['cost_usd','花费（美元）','USD'],
] as const;

export const validMetricValue = (value:unknown):value is number => typeof value === 'number' && Number.isFinite(value);
export function metricReading(value:number|null|undefined, precision?:MetricPrecision, knownValue?:number|null, knownEstimated?:boolean) {
  const partial = precision === 'partial';
  const amount = partial && knownValue !== undefined ? knownValue : value;
  if (precision === 'unavailable' || !validMetricValue(amount)) return null;
  return {value:amount, partial, estimated:precision === 'estimated' || (partial && knownEstimated === true)};
}

export function metricValue(value:number|null|undefined, unit:string, precision?:MetricPrecision) {
  if (!validMetricValue(value) || precision === 'unavailable') return '未记录';
  const prefix = precision === 'estimated' ? '≈ ' : '';
  if (['USD','usd','$'].includes(unit)) return prefix + '$' + value.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:6});
  if (['ratio','percent','%'].includes(unit)) return prefix + (value * 100).toLocaleString('zh-CN',{maximumFractionDigits:1}) + '%';
  if (Math.abs(value) >= 1_000_000) return prefix + (value/1_000_000).toLocaleString('en-US',{maximumFractionDigits:2}) + 'M';
  if (Math.abs(value) >= 10_000) return prefix + (value/1_000).toLocaleString('en-US',{maximumFractionDigits:1}) + 'K';
  return prefix + value.toLocaleString('zh-CN',{maximumFractionDigits:2});
}
export function overviewMetrics(value:SessionOverview|null):OverviewMetric[] {
  const definitions: ReadonlyArray<readonly [string,string,string]> = value?.metrics.some(metric => metric.id === 'legacy_model_records')
    ? [metricDefinitions[0], ['legacy_model_records', '旧版模型执行记录', 'count'], ...metricDefinitions.slice(1)]
    : metricDefinitions;
  return definitions.map(([id,title,unit]) => {
    const metric=value?.metrics.find(item=>item.id===id);
    return {...metric,id,title:metric?.title||title,unit,value:metric?.value??null,precision:metric?.precision??'unavailable',note:metric?.note||'尚无可用记录。'};
  });
}

export type TrajectoryKind = 'input'|'model'|'tool';
export type TrajectoryItem = {id:string;kind:TrajectoryKind;at:string|null;title:string;status:string;text?:string;text_truncated?:boolean;details?:Record<string,unknown>};
export type TrajectoryPage = {
  schema:string; conversation_id:string; items:TrajectoryItem[]; next_cursor:string|null; has_more:boolean;
  summary?:{input_count:number;model_count:number;tool_count:number;request_count:number|null;started_at:string|null;ended_at:string|null;tool_call_count?:number|null;turn_count?:number|null};
};
export const trajectoryLabels:Record<TrajectoryKind,string>={input:'输入',model:'模型',tool:'工具'};
export function trajectoryPath(conversationId:string,cursor?:string|null) {
  const params=new URLSearchParams({limit:'100'});
  if(cursor)params.set('cursor',cursor);
  return `/conversations/${encodeURIComponent(conversationId)}/trajectory?${params}`;
}
export function appendTrajectory(previous:TrajectoryItem[],next:TrajectoryItem[]) {
  const values=new Map(previous.map(item=>[item.id,item]));
  for(const item of next)values.set(item.id,item);
  return [...values.values()];
}
export function filterTrajectory(items:TrajectoryItem[],query:string,kind:TrajectoryKind|'all') {
  const needle=query.trim().toLocaleLowerCase();
  return items.filter(item=>(kind==='all'||item.kind===kind)&&(!needle||[item.title,item.text,trajectoryLabels[item.kind]].filter(Boolean).join(' ').toLocaleLowerCase().includes(needle)));
}
export function trajectoryPositions(items:TrajectoryItem[]) {
  const times=items.map(item=>item.at?Date.parse(item.at):NaN);
  const valid=times.filter(Number.isFinite);
  const first=Math.min(...valid),last=Math.max(...valid);
  return items.map((item,index)=>({item,position:3+94*(Number.isFinite(times[index])&&last>first ? (times[index]-first)/(last-first) : index/Math.max(1,items.length-1))}));
}
export function durationText(start?:string|null,end?:string|null) {
  if(!start||!end)return '未记录';
  const seconds=(Date.parse(end)-Date.parse(start))/1000;
  if(!Number.isFinite(seconds)||seconds<0)return '未记录';
  if(seconds<60)return `${Math.round(seconds)} 秒`;
  if(seconds<3600)return `${Math.floor(seconds/60)} 分 ${Math.floor(seconds%60)} 秒`;
  return `${Math.floor(seconds/3600)} 小时 ${Math.floor(seconds%3600/60)} 分`;
}
export function trajectoryTime(value?:string|null) {
  if(!value||!Number.isFinite(Date.parse(value)))return '时间未记录';
  return new Date(value).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
}
export function trajectoryDetails(details?:Record<string,unknown>) {
  const fields=[['model_id','模型'],['model_label','模型名称'],['tool_name','工具'],['duration_ms','用时（毫秒）'],['total_input_tokens','输入 token'],['input_tokens','未缓存输入'],['output_tokens','输出 token'],['cache_read_tokens','缓存命中 token'],['cache_write_tokens','缓存写入 token'],['request_count','模型请求'],['source_count','来源数量'],['error_code','错误代码']] as const;
  return fields.flatMap(([key,label])=>{
    const value=details?.[key];
    return (typeof value==='string'&&value.length<=200 || typeof value==='number'&&Number.isFinite(value))?[{label,value:String(value)}]:[];
  });
}
