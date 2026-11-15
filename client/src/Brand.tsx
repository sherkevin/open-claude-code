const markUrl = `${((import.meta as ImportMeta & { env?: Record<string,string|undefined> }).env?.BASE_URL || '/')}brand/flintworks-mark.png`;

export default function Brand({ name = '开物', compact = false }: { name?: string; compact?: boolean }) {
  return <div className={`brand fw-brand ${compact ? 'compact' : ''}`}>
    <span className="fw-brand-mark" aria-hidden="true" style={{ maskImage: `url("${markUrl}")`, WebkitMaskImage: `url("${markUrl}")` }}/>
    <span className="fw-brand-text">{name.trim() || '开物'}<small>企业业务助手</small></span>
  </div>;
}
