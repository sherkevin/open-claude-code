import './business-icon.css';

const brandBase = `${(import.meta as ImportMeta & { env?: { BASE_URL?: string } }).env?.BASE_URL || '/'}brand/`;
export default function Brand({ name = 'Flint 开物', compact = false }: { name?: string; compact?: boolean }) {
  const productName = name.trim().replace(/^Flint\s+/i, '') || '开物';
  return <div className={`brand fw-brand ${compact ? 'compact' : ''}`}>
    {compact && <img className="flint-app-mark" src={brandBase + 'flint-f.svg'} alt="Flint" width="32" height="32"/>}
    <img className="flint-wordmark" src={brandBase + 'flint-wordmark-light.svg'} alt="Flint" width="120" height="45"/>
    <span className="fw-brand-text">{productName}<small>企业业务助手</small></span>
  </div>;
}
