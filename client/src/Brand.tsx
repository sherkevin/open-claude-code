const markUrl = `${((import.meta as ImportMeta & { env?: Record<string,string|undefined> }).env?.BASE_URL || '/')}brand/flint-wordmark-light.svg`;
export default function Brand({ name = '开物', compact = false }: { name?: string; compact?: boolean }) {
  return <div className={`brand fw-brand ${compact ? 'compact' : ''}`}>
    <img className="flint-wordmark" src={markUrl} alt="Flint" width="120" height="45"/>
    <span className="fw-brand-text">{name.trim() || '开物'}<small>企业业务助手</small></span>
  </div>;
}
