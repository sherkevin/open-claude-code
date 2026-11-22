import { RefreshCw } from 'lucide-react';
import { Button, ErrorNotice } from './components';
import { metricPrecisionLabel, metricValue, overviewMetrics, validMetricValue, type SessionOverview, type SessionUsage } from './sessionObservationData';
import './session-observability.css';

export function SessionOverviewCards({value,loading,error,onRefresh}:{value:SessionOverview|null;loading:boolean;error:string;onRefresh:()=>void}) {
  const metrics=overviewMetrics(value);
  return <section className="session-overview" aria-label="会话概览" aria-busy={loading}>
    <header className="session-overview-heading"><div><h2>会话概览</h2><p>我的全部会话 · 含已归档会话</p></div><Button variant="ghost" busy={loading} onClick={onRefresh} aria-label="刷新会话概览"><RefreshCw size={14}/>刷新</Button></header>
    <ErrorNotice message={error}/>
    <div className="session-metric-grid">{metrics.map(metric=><article className="session-metric" key={metric.id} data-metric={metric.id}>
      <header><span>{metric.title}</span>{metricPrecisionLabel(metric.precision) && <small>{metricPrecisionLabel(metric.precision)}</small>}</header>
      <strong title={validMetricValue(metric.value) && metric.precision !== 'unavailable' ? `${metric.value.toLocaleString('zh-CN')} ${metric.unit}` : '未记录'}>{metricValue(metric.value,metric.unit,metric.precision)}</strong>
      <p title={metric.note}>{metric.note}</p>
    </article>)}</div>
    <details className="session-metric-notes"><summary>指标说明</summary>{value?.coverage.note && <p>{value.coverage.note}</p>}<dl>{metrics.map(metric=><div key={metric.id}><dt>{metric.title}</dt><dd>{metric.note}</dd></div>)}</dl></details>
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
