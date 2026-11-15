const markUrl = `${((import.meta as ImportMeta & { env?: Record<string,string|undefined> }).env?.BASE_URL || '/')}brand/flintworks-mark.png`;

export default function Brand({ name, compact = false }: { name: string; compact?: boolean }) {
  return <div className={`brand fw-brand ${compact ? 'compact' : ''}`}>
    <span className="fw-brand-mark" aria-hidden="true" style={{ maskImage: `url("${markUrl}")`, WebkitMaskImage: `url("${markUrl}")` }}/>
    <span className="fw-brand-text">{name}<small>企业业务工作空间</small></span>
  </div>;
}
