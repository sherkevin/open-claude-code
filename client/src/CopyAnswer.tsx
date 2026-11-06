import { useEffect, useRef, useState } from 'react';
import { Check, Copy } from 'lucide-react';

export default function CopyAnswer({ text }: { text: string }) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle');
  const timer = useRef<ReturnType<typeof setTimeout>>();
  useEffect(() => () => clearTimeout(timer.current), []);
  return <button type="button" className="copy-answer" aria-label={state === 'copied' ? '已复制回答' : '复制回答'} title={state === 'failed' ? '复制失败，请选择文字后复制' : '复制回答'} onClick={async () => {
    try { await navigator.clipboard.writeText(text); setState('copied'); }
    catch { setState('failed'); }
    clearTimeout(timer.current);
    timer.current = setTimeout(() => setState('idle'), 2400);
  }}>{state === 'copied' ? <Check size={14}/> : <Copy size={14}/>}<span role="status">{state === 'copied' ? '已复制' : state === 'failed' ? '请手动复制' : '复制'}</span></button>;
}
