import { Sparkles } from 'lucide-react';

/** Decorative identity; the adjacent text carries the actual assistant/run state. */
export default function AiIdentity({ size = 'medium', active = false }: { size?: 'inline' | 'small' | 'medium' | 'large'; active?: boolean }) {
  return <span className={`fw-ai-identity fw-ai-${size}${active ? ' is-working' : ''}`} aria-hidden="true"><Sparkles size={20} strokeWidth={1.7}/></span>;
}
