import { useEffect, useState } from 'react';
import { Download, Gauge, LockKeyhole, RefreshCw } from 'lucide-react';
import { Button, EmptyState, PageHeader } from './components';
import { api } from './api';

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
  task_results?: BenchmarkTaskResult[];
}

export interface BenchmarkTaskResult {
  id: string;
  status: BenchmarkMetricStatus;
  note?: string;
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
  ablation_switches?: Record<string, Record<string, boolean>>;
  participants: BenchmarkParticipant[];
  token_accounting?: {
    policy: string;
    baseline_input_tokens: number;
    enhanced_input_tokens: number;
    output_tokens: number;
    compression_savings_tokens: number;
    note: string;
  };
  evidence_boundary: string;
  task_results?: BenchmarkTaskResult[];
}

/**
 * Stage 0 is intentionally a local, synthetic snapshot. It is not a claim
 * about the product assistant or a customer system. Future runner reports can
 * be passed to BenchmarkPage without changing the chart contract.
 */
export const syntheticStage0Benchmark: BenchmarkReport = {
  synthetic: true,
  // Keep the fallback snapshot on the same fixture identity as the server API.
  fixture_id: 'synthetic-ablation-industrial-v1',
  fixture_version: '1.1.0',
  seed: 20261107,
  generated_at: '2026-11-07T00:00:00Z',
  comparison_key: '8e3286feb5328ba346834160',
  task_count: 4,
  passed_tasks: 3,
  task_results: [
    { id: 'policy', status: 'measured', note: '固定文本消融任务' },
    { id: 'memory', status: 'measured', note: '固定文本消融任务' },
    { id: 'long-context', status: 'measured', note: '固定文本消融任务' },
    { id: 'entity', status: 'not_measured', note: '需要获批视觉模型和脱敏图片样例' },
  ],
  ablation_switches: {
    'enhanced-agent': { knowledge_retrieval: true, multimodal_graph: true, conversation_memory: true, automatic_model_routing: true, approval_policy: true, subagent_orchestration: true },
    'simple-agent-baseline': { knowledge_retrieval: false, multimodal_graph: false, conversation_memory: false, automatic_model_routing: false, approval_policy: false, subagent_orchestration: false },
  },
  participants: [
    {
      id: 'enhanced-agent',
      label: '演示夹具运行器（增强方案）',
      kind: 'synthetic',
      metrics: [
        { id: 'correctness', label: '任务正确性', value: 100, unit: '%', status: 'measured', note: '3/3 已支持的离线任务通过；图片语义任务待视觉模型' },
        { id: 'source-coverage', label: '来源覆盖率', value: 100, unit: '%', status: 'measured', note: '3/3 已支持任务有演示来源；图片区域实体尚未索引' },
        { id: 'tokens', label: '总 Token（估算）', value: 187, unit: 'tokens', status: 'measured', note: '只计入压缩后送入模型的上下文；压缩过程不计入；未调用模型' },
        { id: 'input-tokens', label: '输入 Token（估算）', value: 153, unit: 'tokens', status: 'measured', note: '只计入压缩后的记忆摘要与来源标记' },
        { id: 'compression-savings', label: '上下文压缩节省 Token', value: 83, unit: 'tokens', status: 'measured', note: '固定文本中原始上下文与压缩上下文的差值；不计入总 Token' },
        { id: 'context-memory', label: '上下文记忆能力', value: 100, unit: '%', status: 'measured', note: '3 项已支持任务的记忆断言通过；图片任务未测' },
        { id: 'context-management', label: '上下文管理能力', value: 100, unit: '%', status: 'measured', note: '已支持任务的检索和来源可追溯；图片任务未测' },
        { id: 'multimodal-evidence', label: '图片语义证据', value: null, unit: '%', status: 'not_measured', note: '图片区域和实体索引需要获批视觉模型' },
        { id: 'latency', label: '模型响应速度', value: null, unit: 'ms', status: 'not_measured', note: '未调用模型；真实速度需授权模型端点' },
        { id: 'local-overhead', label: '本地编排耗时', value: 1, unit: 'ms', status: 'measured', note: '仅为本地演示编排耗时，不等于模型响应速度' },
      ],
    },
    {
      id: 'simple-agent-baseline',
      label: '最简单 Agent（客户现有助手的消融替身）',
      kind: 'synthetic',
      metrics: [
        { id: 'correctness', label: '任务正确性', value: 50, unit: '%', status: 'measured', note: '2/3 已支持的固定任务无需增强即可完成；图片语义任务待视觉模型' },
        { id: 'source-coverage', label: '来源覆盖率', value: 0, unit: '%', status: 'measured', note: '不接入知识库和图谱；图片语义任务待视觉模型' },
        { id: 'tokens', label: '总 Token（估算）', value: 270, unit: 'tokens', status: 'measured', note: '同一固定任务的原始上下文未压缩；未调用模型' },
        { id: 'input-tokens', label: '输入 Token（估算）', value: 236, unit: 'tokens', status: 'measured', note: '同一固定任务的原始上下文完整计入；未调用模型' },
        { id: 'compression-savings', label: '上下文压缩节省 Token', value: 0, unit: 'tokens', status: 'measured', note: '基线不执行上下文压缩' },
        { id: 'context-memory', label: '上下文记忆能力', value: 25, unit: '%', status: 'measured', note: '3 项已支持任务中仅保持当前轮；跨轮记忆失败' },
        { id: 'context-management', label: '上下文管理能力', value: 30, unit: '%', status: 'measured', note: '不压缩、不分段、不管理来源；图片语义任务待视觉模型' },
        { id: 'multimodal-evidence', label: '图片语义证据', value: null, unit: '%', status: 'not_measured', note: '图片区域和实体索引需要获批视觉模型' },
        { id: 'latency', label: '模型响应速度', value: null, unit: 'ms', status: 'not_measured', note: '未调用模型；真实速度需授权模型端点' },
        { id: 'local-overhead', label: '本地编排耗时', value: 0, unit: 'ms', status: 'measured', note: '无增强模块的基线开销' },
      ],
    },
    {
      id: 'customer-assistant',
      label: '客户现有助手（真实数据待授权）',
      kind: 'customer',
      metrics: [
        { id: 'correctness', label: '任务正确性', value: null, unit: '%', status: 'not_measured', note: '需要客户同任务样例与授权端点' },
        { id: 'source-coverage', label: '来源覆盖率', value: null, unit: '%', status: 'not_measured', note: '需要客户同任务样例与授权端点' },
        { id: 'tokens', label: '总 Token', value: null, unit: 'tokens', status: 'not_measured', note: '需要客户同任务样例与授权端点' },
        { id: 'input-tokens', label: '输入 Token', value: null, unit: 'tokens', status: 'not_measured', note: '需要客户同任务样例与授权端点' },
        { id: 'context-memory', label: '上下文记忆能力', value: null, unit: '%', status: 'not_measured', note: '需要客户同任务样例与授权端点' },
        { id: 'context-management', label: '上下文管理能力', value: null, unit: '%', status: 'not_measured', note: '需要客户同任务样例与授权端点' },
        { id: 'latency', label: '模型响应速度', value: null, unit: 'ms', status: 'not_measured', note: '需要客户同任务样例与授权端点' },
        { id: 'local-overhead', label: '本地编排耗时', value: null, unit: 'ms', status: 'not_measured', note: '客户系统未接入' },
      ],
    },
  ],
  token_accounting: {
    policy: '总 Token 只包含任务输入（压缩后上下文）和输出；上下文压缩过程不计入。',
    baseline_input_tokens: 236,
    enhanced_input_tokens: 153,
    output_tokens: 34,
    compression_savings_tokens: 83,
    note: '固定文本估算，不代表供应商账单；本运行器未调用模型。',
  },
  evidence_boundary: '仅使用 synthetic-ablation-industrial-v1 固定中文文本和本地确定性逻辑；图片任务仅登记为待测，不伪造视觉实体或关系。未连接客户系统、生产数据、模型、网络或计费服务。模型响应速度、视觉质量和真实客户助手指标仍待授权端点与同任务样例。',
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
  const [remoteReport, setRemoteReport] = useState<BenchmarkReport | null>(null);
  useEffect(() => {
    if (!admin || report === null) return;
    void api<BenchmarkReport>('/benchmarks/synthetic/ablation').then(setRemoteReport).catch(() => {
      // The local fixture remains visible when an older backend has no route.
    });
  }, [admin, report]);
  const activeReport = remoteReport ?? report;
  if (!admin) {
    return <div className="page"><PageHeader eyebrow="可控的证据边界" title="对标基准" description="对标报告只对有权限的管理员开放。"/><EmptyState icon={<LockKeyhole size={30}/>} title="需要管理员权限" description="请联系工作台管理员查看演示基准和导出报告。"/></div>;
  }
  if (!activeReport) {
    return <div className="page"><PageHeader eyebrow="可控的证据边界" title="对标基准" description="固定任务、数据和工具后，才能比较助手能力。" actions={<Button variant="secondary" disabled><RefreshCw size={16}/>暂无报告</Button>}/><EmptyState icon={<Gauge size={30}/>} title="还没有可视化报告" description="先运行演示基准 runner，再把经过校验的 JSON 报告导入这里。缺失指标不会被估算。"/></div>;
  }
  return <div className="page benchmark-page">
    <PageHeader eyebrow="用证据比较能力" title="对标基准" description="固定演示任务的安全可视化；客户真实助手和未测指标保持待测。" actions={<Button variant="secondary" onClick={() => downloadBenchmarkReport(activeReport)}><Download size={16}/>导出 JSON</Button>}/>
    <div className="benchmark-notice"><Gauge size={17}/><div><strong>演示消融证据 · {activeReport.fixture_id} v{activeReport.fixture_version}</strong><p>种子 {activeReport.seed} · 对比键 <code>{activeReport.comparison_key}</code> · {activeReport.passed_tasks}/{activeReport.task_count} 项离线任务通过</p></div><span className="badge green">synthetic only</span></div>
    {activeReport.task_results && <section className="benchmark-task-coverage" aria-label="固定任务证据范围"><div className="section-label"><span>固定任务证据范围</span><span>图片语义任务未测</span></div><div className="benchmark-task-list">{activeReport.task_results.map(task => <div className={`benchmark-task ${task.status}`} key={task.id}><span>{task.id}</span><strong>{task.status === 'measured' ? '已测' : '待测'}</strong>{task.note && <small>{task.note}</small>}</div>)}</div></section>}
    {activeReport.ablation_switches && <div className="benchmark-switches" aria-label="消融开关"><span>增强开关</span>{Object.entries(activeReport.ablation_switches['enhanced-agent'] ?? {}).map(([name, enabled]) => <code key={name} className={enabled ? 'on' : ''}>{name}</code>)}</div>}
    <section className="benchmark-chart-card" aria-labelledby="benchmark-chart-heading">
      <header className="section-label"><span id="benchmark-chart-heading">能力指标对比</span><span>缺失数据显示“待测”</span></header>
      <p className="section-help">本地演示消融实验，不是客户真实调用。增强方案只把压缩后的上下文计入 Token，压缩过程不计入；模型速度没有授权端点时保持“待测”。</p>
      <div className="benchmark-legend" aria-label="图例"><span><i className="benchmark-legend-swatch synthetic"/>增强方案</span><span><i className="benchmark-legend-swatch baseline"/>最简单 Agent 基线</span><span><i className="benchmark-legend-swatch pending"/>待测</span></div>
      <BenchmarkBars participants={activeReport.participants}/>
    </section>
    <section className="benchmark-metric-grid" aria-label="详细指标">
      {activeReport.participants.map(participant => <article className="benchmark-participant" key={participant.id}><header><span className={`benchmark-participant-dot ${participant.kind}`}/><div><h2>{participant.label}</h2><small>{participant.kind === 'synthetic' ? '固定夹具 · 可重复' : '尚未接入 · 不含客户数据'}</small></div></header><dl>{participant.metrics.map(metric => <div key={metric.id}><dt>{metric.label}</dt><dd className={metric.status === 'not_measured' ? 'pending' : ''}>{metricDisplay(metric)}</dd>{metric.note && <small>{metric.note}</small>}</div>)}</dl></article>)}
    </section>
    <p className="benchmark-boundary">证据边界：{activeReport.evidence_boundary}</p>
  </div>;
}

function BenchmarkBars({ participants }: { participants: BenchmarkParticipant[] }) {
  const metrics = [
    { id: 'correctness', label: '任务正确性（越高越好）' },
    { id: 'tokens', label: 'Token 消耗（越低越好）' },
    { id: 'context-memory', label: '上下文记忆能力（越高越好）' },
    { id: 'context-management', label: '上下文管理能力（越高越好）' },
    { id: 'local-overhead', label: '本地编排耗时（越低越好，非模型速度）' },
    { id: 'latency', label: '模型响应速度（真实模型待测）' },
  ];
  return <div className="benchmark-bars" role="img" aria-label="任务正确性、Token消耗、上下文能力和模型速度对比柱状图">{metrics.map(metric => {
    const measuredValues = participants.map(participant => metricById(participant, metric.id)).filter(value => value?.status === 'measured' && typeof value.value === 'number' && Number.isFinite(value.value)).map(value => value!.value as number);
    const max = Math.max(...measuredValues, 1);
    return <div className="benchmark-bar-group" key={metric.id}><div className="benchmark-bar-label"><strong>{metric.label}</strong><span>{metric.id === 'tokens' ? 'Token' : ['latency', 'local-overhead'].includes(metric.id) ? '毫秒' : '百分比'}</span></div>{participants.map(participant => { const value = metricById(participant, metric.id); const measured = value?.status === 'measured' && typeof value.value === 'number' && Number.isFinite(value.value); const width = measured ? (metric.id === 'tokens' ? (value.value! / max) * 100 : Math.max(0, Math.min(100, value.value!))) : 0; return <div className="benchmark-bar-row" key={participant.id}><span>{participant.label}</span><div className="benchmark-bar-track"><div className={`benchmark-bar ${participant.kind} ${participant.id === 'simple-agent-baseline' ? 'baseline' : ''} ${measured ? '' : 'pending'}`} style={{ width: `${width}%` }}/></div><strong>{metricDisplay(value)}</strong></div>;})}</div>;
  })}</div>;
}
