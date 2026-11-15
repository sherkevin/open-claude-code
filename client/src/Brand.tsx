const markUrl = `${((import.meta as ImportMeta & { env?: Record<string,string|undefined> }).env?.BASE_URL || '/')}brand/flint-wordmark-light.svg`;
export default function Brand({ name = 'Flint 开物', compact = false }: { name?: string; compact?: boolean }) {
  const productName = name.trim().replace(/^Flint\s+/i, '') || '开物';
  return <div className={`brand fw-brand ${compact ? 'compact' : ''}`}>
    <img className="flint-wordmark" src={markUrl} alt="Flint" width="120" height="45"/>
    <span className="fw-brand-text">{productName}<small>企业业务助手</small></span>
  </div>;
}
