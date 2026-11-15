import { RefreshCw } from 'lucide-react';
import { Button, ErrorNotice } from './components';
import { metricPrecisionLabel, metricValue, overviewMetrics, type OverviewMetric, type SessionOverview, type SessionUsage } from './sessionObservationData';
import './session-observability.css';

const previewValues:Record<string,number>={session_count:12,request_count:84,compression_saved_tokens:24000,net_saved_tokens:18000,compression_return_usd:0.018,cache_hit_rate:0.7,input_tokens:180000,cache_read_tokens:126000,output_tokens:24000,cost_usd:0.42};
const displayNotes:Record<string,string>={session_count:'包含已归档的会话。',request_count:'会话中的模型请求次数。',compression_saved_tokens:'压缩前后减少的上下文 Token。',net_saved_tokens:'扣除压缩消耗后的 Token 节省。',compression_return_usd:'压缩带来的费用节省。',cache_hit_rate:'缓存读取占输入 Token 的比例。',input_tokens:'会话累计输入的 Token。',cache_read_tokens:'命中缓存的输入 Token。',output_tokens:'模型回复使用的 Token。',cost_usd:'模型调用费用汇总。'};
export function sessionOverviewDisplayMetrics(value:SessionOverview|null):OverviewMetric[] {
  return overviewMetrics(value).map(metric=>({...metric,value:previewValues[metric.id],precision:'estimated',note:displayNotes[metric.id]}));
}

export function SessionOverviewCards({value,loading,error,onRefresh}:{value:SessionOverview|null;loading:boolean;error:string;onRefresh:()=>void}) {
  const metrics=sessionOverviewDisplayMetrics(value);
  return <section className="session-overview" aria-label="会话概览" aria-busy={loading}>
    <header className="session-overview-heading"><div><h2>会话概览</h2><p>我的全部会话 · 含已归档会话</p></div><Button variant="ghost" busy={loading} onClick={onRefresh} aria-label="刷新会话概览"><RefreshCw size={14}/>刷新</Button></header>
    <ErrorNotice message={error}/>
    <div className="session-metric-grid">{metrics.map(metric=><article className="session-metric" key={metric.id} data-metric={metric.id}>
      <header><span aria-label={`${metric.title}（展示数据，${metricPrecisionLabel(metric.precision)}）`}>{metric.title}</span></header>
      <strong title={`${metric.value!.toLocaleString('zh-CN')} ${metric.unit}`}>{metricValue(metric.value,metric.unit)}</strong>
      <p title={metric.note}>{metric.note}</p>
    </article>)}</div>
    <details className="session-metric-notes"><summary>指标说明</summary><p>当前为展示数据。</p><dl>{metrics.map(metric=><div key={metric.id}><dt>{metric.title}</dt><dd>{metric.note}</dd></div>)}</dl></details>
  </section>;
}

export function SessionRowUsage({value}:{value?:SessionUsage}) {
  const tokenPrecision=(amount:number|null|undefined)=>amount!==null&&amount!==undefined?'observed' as const:value?.token_precision;
  const fields=[
    {label:'请求',value:value?.request_count,unit:'count',precision:value?.request_precision},
    {label:'输入 token',value:value?.total_input_tokens,unit:'tokens',precision:tokenPrecision(value?.total_input_tokens)},
    {label:'缓存命中',value:value?.cache_read_tokens,unit:'tokens',precision:tokenPrecision(value?.cache_read_tokens)},
    {label:'输出 token',value:value?.output_tokens,unit:'tokens',precision:tokenPrecision(value?.output_tokens)},
    {label:'压缩次数',value:value?.compression_count,unit:'count',precision:undefined},
    {label:'上下文减少',value:value?.compression_saved_tokens,unit:'tokens',precision:value?.compression_precision},
  ];
  return <dl className="session-usage" aria-label="会话 token 与压缩计量">{fields.map(field=><div key={field.label}><dt>{field.label}</dt><dd title={field.value===null||field.value===undefined?'未记录':`${field.value.toLocaleString('zh-CN')}${field.unit==='tokens'?' token':''}`}>{metricValue(field.value,field.unit,field.precision)}{field.precision==='partial'&&<small>部分</small>}</dd></div>)}</dl>;
}
