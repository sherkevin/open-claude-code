import { Download, Gauge, LockKeyhole, RefreshCw } from 'lucide-react';
import { Button, EmptyState, PageHeader } from './components';

export type BenchmarkMetricStatus = 'measured' | 'not_measured';

export interface BenchmarkMetric {
  id: string;
  label: string;
  value: number | null;
  unit: '%' | 'ms' | 'tokens' | 'usd' | null;
  status: BenchmarkMetricStatus;
  note?: string;
}

export interface BenchmarkParticipant {
  id: string;
  label: string;
  kind: 'synthetic' | 'customer';
  metrics: BenchmarkMetric[];
}

export interface BenchmarkReport {
  synthetic: true;
  fixture_id: string;
  fixture_version: string;
  seed: number;
  generated_at: string;
  comparison_key: string;
  task_count: number;
  passed_tasks: number;
  participants: BenchmarkParticipant[];
  evidence_boundary: string;
}

/**
 * Stage 0 is intentionally a local, synthetic snapshot. It is not a claim
 * about the product assistant or a customer system. Future runner reports can
 * be passed to BenchmarkPage without changing the chart contract.
 */
export const syntheticStage0Benchmark: BenchmarkReport = {
  synthetic: true,
  fixture_id: 'synthetic-industrial-v1',
  fixture_version: '1.0.0',
  seed: 20261106,
  generated_at: '2026-11-06T00:00:00Z',
  comparison_key: '355b2398f15d812b1cbcf3f0',
  task_count: 4,
  passed_tasks: 4,
  participants: [
    {
      id: 'synthetic-runner',
      label: '合成夹具运行器',
      kind: 'synthetic',
      metrics: [
        { id: 'correctness', label: '任务正确性', value: 100, unit: '%', status: 'measured', note: '4/4 离线任务通过' },
        { id: 'source-coverage', label: '来源覆盖率', value: 100, unit: '%', status: 'measured', note: '每项任务均有合成来源' },
        { id: 'latency', label: '响应耗时', value: null, unit: 'ms', status: 'not_measured', note: '本阶段没有产品或模型响应' },
        { id: 'tokens', label: 'Token', value: null, unit: 'tokens', status: 'not_measured', note: '未调用模型' },
        { id: 'context', label: '上下文', value: null, unit: null, status: 'not_measured', note: '未创建模型会话' },
        { id: 'multimodal', label: '多模态质量', value: null, unit: null, status: 'not_measured', note: '仅登记图片来源' },
      ],
    },
    {
      id: 'customer-assistant',
      label: '客户现有助手',
      kind: 'customer',
      metrics: [
        { id: 'correctness', label: '任务正确性', value: null, unit: '%', status: 'not_measured', note: '等待客户授权和同任务样例' },
        { id: 'source-coverage', label: '来源覆盖率', value: null, unit: '%', status: 'not_measured' },
        { id: 'latency', label: '响应耗时', value: null, unit: 'ms', status: 'not_measured' },
        { id: 'tokens', label: 'Token', value: null, unit: 'tokens', status: 'not_measured' },
        { id: 'context', label: '上下文', value: null, unit: null, status: 'not_measured' },
        { id: 'multimodal', label: '多模态质量', value: null, unit: null, status: 'not_measured' },
      ],
    },
  ],
  evidence_boundary: '仅使用 synthetic-industrial-v1 合成夹具；未连接客户系统、生产数据、模型、网络或计费服务。',
};

export function serializeBenchmarkReport(report: BenchmarkReport): string {
  return `${JSON.stringify(report, null, 2)}\n`;
}

export function downloadBenchmarkReport(report: BenchmarkReport): boolean {
  if (typeof document === 'undefined' || typeof URL === 'undefined' || typeof URL.createObjectURL !== 'function') return false;
  const blob = new Blob([serializeBenchmarkReport(report)], { type: 'application/json;charset=utf-8' });
  const href = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = href;
  anchor.download = `${report.fixture_id}-${report.fixture_version}-benchmark.json`;
  anchor.click();
  window.setTimeout(() => URL.revokeObjectURL(href), 0);
  return true;
}

const metricById = (participant: BenchmarkParticipant, id: string) => participant.metrics.find(metric => metric.id === id);
const metricDisplay = (metric: BenchmarkMetric | undefined) => {
  if (!metric || metric.status !== 'measured' || metric.value === null || !Number.isFinite(metric.value)) return '待测';
  if (metric.unit === '%') return `${metric.value}%`;
  if (metric.unit === 'ms') return `${metric.value} 毫秒`;
  if (metric.unit === 'tokens') return `${metric.value.toLocaleString('zh-CN')} Token`;
  if (metric.unit === 'usd') return `$${metric.value.toFixed(4)}`;
  return String(metric.value);
};

export default function BenchmarkPage({ admin, report = syntheticStage0Benchmark }: { admin: boolean; report?: BenchmarkReport | null }) {
  if (!admin) {
    return <div className="page"><PageHeader eyebrow="可控的证据边界" title="对标基准" description="对标报告只对有权限的管理员开放。"/><EmptyState icon={<LockKeyhole size={30}/>} title="需要管理员权限" description="请联系工作台管理员查看合成基准和导出报告。"/></div>;
  }
  if (!report) {
    return <div className="page"><PageHeader eyebrow="可控的证据边界" title="对标基准" description="固定任务、数据和工具后，才能比较助手能力。" actions={<Button variant="secondary" disabled><RefreshCw size={16}/>暂无报告</Button>}/><EmptyState icon={<Gauge size={30}/>} title="还没有可视化报告" description="先运行合成基准 runner，再把经过校验的 JSON 报告导入这里。缺失指标不会被估算。"/></div>;
  }
  return <div className="page benchmark-page">
    <PageHeader eyebrow="用证据比较能力" title="对标基准" description="固定合成任务的安全可视化；客户真实助手和未测指标保持待测。" actions={<Button variant="secondary" onClick={() => downloadBenchmarkReport(report)}><Download size={16}/>导出 JSON</Button>}/>
    <div className="benchmark-notice"><Gauge size={17}/><div><strong>合成证据 · {report.fixture_id} v{report.fixture_version}</strong><p>种子 {report.seed} · 对比键 <code>{report.comparison_key}</code> · {report.passed_tasks}/{report.task_count} 项离线任务通过</p></div><span className="badge green">synthetic</span></div>
    <section className="benchmark-chart-card" aria-labelledby="benchmark-chart-heading">
      <header className="section-label"><span id="benchmark-chart-heading">能力指标对比</span><span>缺失数据显示“待测”</span></header>
      <p className="section-help">当前已测数值只来自离线夹具运行器，不代表生产助手或客户效果。柱状图仅显示百分比指标，其他指标保留明确状态。</p>
      <BenchmarkBars participants={report.participants}/>
    </section>
    <section className="benchmark-metric-grid" aria-label="详细指标">
      {report.participants.map(participant => <article className="benchmark-participant" key={participant.id}><header><span className={`benchmark-participant-dot ${participant.kind}`}/><div><h2>{participant.label}</h2><small>{participant.kind === 'synthetic' ? '固定夹具 · 可重复' : '尚未接入 · 不含客户数据'}</small></div></header><dl>{participant.metrics.map(metric => <div key={metric.id}><dt>{metric.label}</dt><dd className={metric.status === 'not_measured' ? 'pending' : ''}>{metricDisplay(metric)}</dd>{metric.note && <small>{metric.note}</small>}</div>)}</dl></article>)}
    </section>
    <p className="benchmark-boundary">证据边界：{report.evidence_boundary}</p>
  </div>;
}

function BenchmarkBars({ participants }: { participants: BenchmarkParticipant[] }) {
  const metrics = [
    { id: 'correctness', label: '任务正确性' },
    { id: 'source-coverage', label: '来源覆盖率' },
  ];
  return <div className="benchmark-bars" role="img" aria-label="任务正确性和来源覆盖率柱状图">{metrics.map(metric => <div className="benchmark-bar-group" key={metric.id}><div className="benchmark-bar-label"><strong>{metric.label}</strong><span>百分比</span></div>{participants.map(participant => { const value = metricById(participant, metric.id); const measured = value?.status === 'measured' && typeof value.value === 'number' && Number.isFinite(value.value); return <div className="benchmark-bar-row" key={participant.id}><span>{participant.label}</span><div className="benchmark-bar-track"><div className={`benchmark-bar ${participant.kind} ${measured ? '' : 'pending'}`} style={{ width: measured ? `${Math.max(0, Math.min(100, value.value!))}%` : '0%' }}/></div><strong>{metricDisplay(value)}</strong></div>;})}</div>)}</div>;
}
