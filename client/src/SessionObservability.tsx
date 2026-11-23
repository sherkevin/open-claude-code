import { RefreshCw } from 'lucide-react';
import { Button, ErrorNotice, Loader } from './components';
import { metricReading, metricValue, overviewMetrics, type SessionOverview, type SessionUsage } from './sessionObservationData';
import './session-observability.css';

type Reading = NonNullable<ReturnType<typeof metricReading>>;

function PartialLabel({ unit }: { unit: string }) {
  const label = unit === 'ratio' ? '按已记录用量计算' : unit === 'USD' ? '已记录金额' : '已记录小计';
  return <small className="metric-precision partial">{label}</small>;
}

function readingText(reading: Reading, unit: string, exactCount = false) {
  if (exactCount && (unit === 'tokens' || unit === 'count')) {
    return (reading.estimated ? '≈ ' : '') + reading.value.toLocaleString('zh-CN');
  }
  return metricValue(reading.value, unit, reading.estimated ? 'estimated' : undefined);
}

export function SessionOverviewCards({ value, loading, error, onRefresh }: {
  value: SessionOverview | null; loading: boolean; error: string; onRefresh: () => void;
}) {
  const ready = !loading && !error;
  const metrics = ready ? overviewMetrics(value).map(metric => ({
    ...metric,
    reading: metricReading(metric.value, metric.precision, metric.known_value, metric.known_estimated),
  })) : [];
  const available = metrics.flatMap(metric => metric.reading ? [{ ...metric, reading: metric.reading }] : []);

  return <section className="session-overview" aria-label="会话概览">
    <header className="session-overview-heading">
      <div><h2>会话概览</h2><p>我的全部会话 · 含已归档会话</p></div>
      <Button variant="ghost" busy={loading} aria-busy={loading} onClick={onRefresh} aria-label="刷新会话概览">
        <RefreshCw size={14} aria-hidden="true"/>刷新
      </Button>
    </header>
    {loading && <div className="session-overview-loading" role="status"><Loader text="正在加载会话概览…"/></div>}
    {!loading && <ErrorNotice message={error}/>}
    {ready && <>
      {available.length === 0 && <p className="session-overview-empty">暂无会话用量记录。</p>}
      {available.length > 0 && <div className="session-metric-grid">
        {available.map(({ id, title, unit, note, reading }) => {
          const displayTitle = reading.partial && id === 'request_count' ? '模型请求' :
            reading.partial && id === 'cache_hit_rate' ? '缓存命中率' : title;
          return <article className="session-metric" key={id} data-metric={id}>
            <header>
              <span>{displayTitle}</span>
              {reading.partial && <PartialLabel unit={unit}/>}
              {reading.estimated && <small className="metric-precision estimated">估算</small>}
            </header>
            <strong title={`${reading.value.toLocaleString('zh-CN')} ${unit}`}>{readingText(reading, unit, true)}</strong>
            <p title={note}>{note}</p>
          </article>;
        })}
      </div>}
      <details className="session-metric-notes">
        <summary>指标说明</summary>
        {value?.coverage.note && <p>{value.coverage.note}</p>}
        <dl>{metrics.map(({ id, title, note, reading }) => <div key={id}>
          <dt>{title}{!reading && <small className="metric-precision">未记录</small>}</dt>
          <dd>{note}</dd>
        </div>)}</dl>
      </details>
    </>}
  </section>;
}

export function SessionRowUsage({ value }: { value?: SessionUsage }) {
  const fields = [
    ['request_count', '请求', 'count', value?.request_precision],
    ['legacy_model_records', '旧版模型记录', 'count'],
    ['total_input_tokens', '输入 token', 'tokens'],
    ['cache_read_tokens', '缓存命中', 'tokens'],
    ['output_tokens', '输出 token', 'tokens'],
    ['compression_count', '压缩次数', 'count'],
    ['compression_saved_tokens', '上下文减少', 'tokens', value?.compression_precision],
  ] as const;
  const available = fields.flatMap(([key, label, unit, precision]) => {
    const reading = metricReading(value?.[key], precision) ??
      metricReading(null, 'partial', value?.known_values?.[key], value?.known_estimated_fields?.includes(key));
    if (!reading) return [];
    const fullValue = `${reading.value.toLocaleString('zh-CN')}${unit === 'tokens' ? ' token' : ''}`;
    return <div key={key}>
      <dt>{label}</dt>
      <dd title={fullValue}>
        {readingText(reading, unit)}
        {reading.partial && <PartialLabel unit={unit}/>}
        {reading.estimated && <small className="metric-precision estimated">估算</small>}
      </dd>
    </div>;
  });
  if (!available.length) return <p className="session-usage session-usage-empty">暂无用量记录。</p>;
  return <dl className="session-usage" aria-label="会话 token 与压缩计量">{available}</dl>;
}
