import { useState } from 'react';
import { AnswerMarkdown } from './Sources';
import './markdown-document.css';

export function isMarkdownFile(file: { name: string; mime_type: string }) {
  return /^text\/(?:x-)?markdown(?:;|$)/i.test(file.mime_type) || /\.(?:md|markdown)$/i.test(file.name);
}

/** A document view; it shares the chat renderer's HTML, URL and image rules. */
export default function MarkdownDocument({ text, onChange, label = 'Markdown 内容', disabled = false, initialView = 'read' }: {
  text: string; onChange?: (value: string) => void; label?: string; disabled?: boolean; initialView?: 'read' | 'source';
}) {
  const [view, setView] = useState(initialView);
  return <section className="markdown-document" aria-label={label}>
    <div className="document-view-switch" role="group" aria-label={`${label}显示方式`}>
      <button type="button" aria-pressed={view === 'read'} onClick={() => setView('read')}>阅读</button>
      <button type="button" aria-pressed={view === 'source'} onClick={() => setView('source')}>{onChange ? '源码编辑' : '源码'}</button>
    </div>
    {view === 'read' ? <div className="document-reading">{text ? <AnswerMarkdown text={text}/> : <p className="section-help">暂无内容</p>}</div>
      : onChange ? <textarea className="document-source" aria-label={label} value={text} onChange={event => onChange(event.target.value)} disabled={disabled} rows={14} spellCheck={false}/>
      : <pre className="document-source text-preview">{text || '暂无内容'}</pre>}
  </section>;
}
