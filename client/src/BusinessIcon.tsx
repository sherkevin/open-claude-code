import './business-icon.css';

const assetBase = (import.meta as ImportMeta & { env?: { BASE_URL?: string } }).env?.BASE_URL || '/';

const iconCategories = [
  ['work-order-search', /工单|(?:^|[^a-z])(?:work[\s_-]?orders?|tickets?)(?:[^a-z]|$)/i],
  ['inventory', /库存|盘点|(?:^|[^a-z])(?:inventor(?:y|ies)|stock)(?:[^a-z]|$)/i],
  ['source-trace', /追溯|溯源|公司方案解读|(?:^|[^a-z])(?:traceability|source[\s_-]?trace|lineage)(?:[^a-z]|$)/i],
  ['production-schedule', /排产|排程|(?:^|[^a-z])(?:production[\s_-]?schedul(?:e|ing)|schedul(?:e|ing)[\s_-]?production)(?:[^a-z]|$)/i],
  ['material-pick', /领料|拣料|(?:^|[^a-z])(?:material[\s_-]?pick(?:ing)?|pick[\s_-]?materials?)(?:[^a-z]|$)/i],
  ['quality-inspect', /质检|质量检验|工业异常分析|(?:^|[^a-z])(?:quality|inspection|yield)(?:[^a-z]|$)/i],
  ['equipment-status', /设备|机台|(?:^|[^a-z])(?:equipment|machines?)(?:[^a-z]|$)/i],
  ['alerts', /告警|报警|预警|(?:^|[^a-z])(?:alerts?|alarms?)(?:[^a-z]|$)/i],
  ['sales-orders', /订单|发货|(?:^|[^a-z])(?:orders?|shipments?)(?:[^a-z]|$)/i],
  ['approval', /审批|审核|(?:^|[^a-z])(?:approvals?|approve)(?:[^a-z]|$)/i],
  ['reports', /报表|报告|(?:^|[^a-z])(?:reports?|dashboards?)(?:[^a-z]|$)/i],
  ['data-connect', /数据连接|数据源|(?:^|[^a-z])data[\s_-]?(?:connect(?:ion)?|sources?)(?:[^a-z]|$)/i],
] as const;

export type BusinessIconName = (typeof iconCategories)[number][0];

export function businessIconFor(name: string, description = ''): BusinessIconName | undefined {
  for (const text of [name, description]) {
    const words = text.normalize('NFKC').replace(/([a-z])([A-Z])/g, '$1 $2');
    const match = iconCategories.find(([, pattern]) => pattern.test(words));
    if (match) return match[0];
  }
}

export function BusinessIcon({ icon, size = 24, mono = false }: { icon: BusinessIconName; size?: number; mono?: boolean }) {
  return <img className="flint-business-icon" src={`${assetBase}brand/business/${mono ? 'mono/' : ''}${icon}.svg`}
    alt="" aria-hidden="true" width={size} height={size}/>;
}
