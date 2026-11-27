import { useEffect, useRef, useState } from 'react';
import { api, ApiError, errorText } from './api';
import { Button, ErrorNotice, Field } from './components';

interface Hit {
  source_id: string;
  source_path: string;
  revision: number;
  text: string;
}

export default function SemanticMemoryPanel({ conversationId, revision, locked }: {
  conversationId: string;
  revision: number;
  locked: boolean;
}) {
  const path = `/conversations/${encodeURIComponent(conversationId)}/memory/semantic`;
  const [available, setAvailable] = useState(false);
  const [query, setQuery] = useState('');
  const [busy, setBusy] = useState<'sync' | 'query' | null>(null);
  const [hits, setHits] = useState<Hit[]>([]);
  const [notice, setNotice] = useState('');
  const [resultRevision, setResultRevision] = useState(revision);
  const [error, setError] = useState('');
  const active = useRef<AbortController | null>(null);

  useEffect(() => {
    const status = new AbortController();
    api<{ enabled: boolean; configured: boolean; mode: string | null }>(
      path + '/status', { signal: status.signal }
    ).then(result => {
      if (!status.signal.aborted) setAvailable(result.enabled && result.configured && result.mode === 'scoped');
    }).catch(() => {}); // Native files remain usable whenever the optional capability is unavailable.
    return () => { status.abort(); active.current?.abort(); };
  }, [path]);

  async function submit(action: 'sync' | 'query') {
    if (active.current || locked || (action === 'query' && !query.trim())) return;
    const request = new AbortController();
    active.current = request;
    setBusy(action);
    setResultRevision(revision);
    setError('');
    setNotice('');
    setHits([]);
    try {
      const init = { method: 'POST', signal: request.signal };
      if (action === 'sync') {
        const result = await api<{ status: string }>(path + '/sync', init);
        if (request.signal.aborted) return;
        if (result.status === 'indexed' || result.status === 'unchanged') {
          setNotice('已同步此会话保存的记忆。');
        } else {
          setNotice('部分记忆的同步结果尚未确认。你仍可查看已保存的文件。');
        }
      } else {
        const result = await api<{ status: string; hits: Hit[] }>(
          path + '/query', { ...init, body: JSON.stringify({ q: query.trim() }) }
        );
        if (request.signal.aborted) return;
        if (result.status === 'verified') {
          setHits(result.hits);
          setNotice(result.hits.length ? '' : '本次检索没有命中。');
        } else {
          setNotice('检索暂未取得可核验的结果。你可以直接查看上方的记忆文件。');
        }
      }
    } catch (cause) {
      if (request.signal.aborted) return;
      if (cause instanceof ApiError && cause.code === 'MEM0_DISABLED') setAvailable(false);
      else setError(errorText(cause));
    } finally {
      if (!request.signal.aborted) setBusy(null);
      active.current = null;
    }
  }

  if (!available) return null;
  return <section className="memory-editor" aria-label="检索会话记忆">
    <div className="memory-editor-heading">
      <h2>检索会话记忆</h2>
      <Button variant="secondary" disabled={locked || !!busy} busy={busy === 'sync'}
        onClick={() => void submit('sync')}>同步已保存的记忆</Button>
    </div>
    <form onSubmit={event => { event.preventDefault(); void submit('query'); }}>
      <Field label="检索内容">
        <input value={query} maxLength={4000} disabled={!!busy}
          onChange={event => { setQuery(event.target.value); setHits([]); setNotice(''); setError(''); }}/>
      </Field>
      <Button type="submit" disabled={locked || !!busy || !query.trim()} busy={busy === 'query'}>检索记忆</Button>
    </form>
    <ErrorNotice message={resultRevision === revision ? error : ''}/>
    <div aria-live="polite">
      {resultRevision === revision && notice && <p>{notice}</p>}
      {resultRevision === revision && hits.map(hit => <article key={hit.source_id}>
        <small>{hit.source_path} · 版本 {hit.revision}</small><p style={{ whiteSpace: 'pre-wrap' }}>{hit.text}</p>
      </article>)}
    </div>
  </section>;
}
