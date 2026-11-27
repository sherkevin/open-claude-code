import { useEffect, useRef, useState } from 'react';
import { Button } from './components';

export type ChatEngineeringEntry = { text: string; ordinaryPending: boolean };
const limit = 20000;

export function appendEngineeringText(existing: string, incoming: string) {
  return existing && incoming ? existing + '\n' + incoming : existing || incoming;
}

type Props = {
  entry: ChatEngineeringEntry; ready: boolean; occupied: boolean; canEdit: boolean; revision: string; text: string;
  edit: (revision: string, text: string) => Promise<boolean>;
};
export function CodexDraftImport({ entry, ready, occupied, canEdit, revision, text, edit }: Props) {
  const [done, setDone] = useState(false);
  const [applying, setApplying] = useState(false);
  const attemptedAuto = useRef(false);
  const writing = useRef(false);
  useEffect(() => {
    if (attemptedAuto.current || !ready) return;
    attemptedAuto.current = true;
    if (!entry.text || entry.ordinaryPending || occupied || !canEdit || entry.text.length > limit) return;
    if (text === entry.text) { setDone(true); return; }
    if (text) return;
    writing.current = true;
    setApplying(true);
    void edit(revision, entry.text).then(accepted => { if (accepted) setDone(true); })
      .finally(() => { writing.current = false; setApplying(false); });
  }, [ready, occupied, canEdit, entry, text, revision, edit]);

  if (done || !entry.text || !ready) return null;
  if (entry.ordinaryPending) return <p className="section-help">普通聊天有待核对的提交。原文保留在普通输入框中，请先核对普通聊天的结果。</p>;
  const combined = appendEngineeringText(text, entry.text);
  async function append() {
    if (writing.current || !canEdit || combined.length > limit) return;
    writing.current = true;
    setApplying(true);
    try { if (await edit(revision, combined)) setDone(true); }
    finally { writing.current = false; setApplying(false); }
  }
  return <section aria-label="来自普通输入框的文字" className="section-help">
    <strong>来自普通输入框的文字</strong>
    <p style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{entry.text}</p>
    {!canEdit ? <p>请先核对原工程请求。带入的文字仍在普通输入框中。</p> : combined.length > limit
      ? <p>合并后的工程草稿超过 20,000 个字符。请先缩短文字。</p>
      : <Button type="button" variant="secondary" disabled={applying} onClick={() => void append()}>追加到工程草稿</Button>}
  </section>;
}
