import { useEffect, useState } from 'react';
import { api, errorText, post } from './api';
import { Button, ErrorNotice } from './components';
import { AnswerMarkdown } from './Sources';

type Interaction = { id: string; kind: 'question' | 'plan'; prompt: string; options: string[] };

export default function RunInteractions({ runId }: { runId: string }) {
  const [items, setItems] = useState<Interaction[]>([]);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      try {
        const result = await api<Interaction[]>(`/runs/${runId}/interactions`, { signal: controller.signal });
        if (!controller.signal.aborted) setItems(result);
      } catch { /* The next request will recover from a transient read failure. */ }
      if (!controller.signal.aborted) timer = setTimeout(refresh, 1000);
    };
    void refresh();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [runId]);

  return items.map(item => <InteractionCard key={item.id} runId={runId} item={item} />);
}

function InteractionCard({ runId, item }: { runId: string; item: Interaction }) {
  const [answer, setAnswer] = useState('');
  const [sent, setSent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const plan = item.kind === 'plan';

  async function respond(text: string) {
    if (busy || !text.trim()) return;
    setBusy(true);
    setError('');
    try {
      await post(`/runs/${runId}/interactions/${item.id}`, { answer: text });
      setSent(true);
    } catch (failure) {
      setError(errorText(failure));
    } finally {
      setBusy(false);
    }
  }

  if (sent) return null;
  return <section className="run-outcome run-interaction" aria-label={plan ? '审阅计划' : '回答助手问题'}>
    <strong>{plan ? '审阅计划' : '助手需要确认'}</strong>
    {plan ? <div className="interaction-plan"><AnswerMarkdown text={item.prompt} /></div> : <p>{item.prompt}</p>}
    <div className="interaction-options">
      {item.options.map(option => <Button key={option} variant="secondary" disabled={busy} onClick={() => void respond(option)}>{option}</Button>)}
      {plan && <Button variant="secondary" disabled={busy} onClick={() => void respond('确认计划')}>确认计划</Button>}
    </div>
    <form onSubmit={event => { event.preventDefault(); void respond(plan ? `修改计划：${answer.trim()}` : answer.trim()); }}>
      <input aria-label={plan ? '修改意见' : '自定义回答'} placeholder={plan ? '写下修改意见' : '也可以输入自己的答案'} value={answer} maxLength={plan ? 1995 : 2000} onChange={event => setAnswer(event.target.value)} />
      <Button type="submit" disabled={!answer.trim()} busy={busy}>{plan ? '要求修改' : '回答'}</Button>
    </form>
    <ErrorNotice message={error} />
  </section>;
}
