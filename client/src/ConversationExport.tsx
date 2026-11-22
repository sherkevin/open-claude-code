import { useState } from 'react';
import { Copy, Download } from 'lucide-react';
import { api, apiURL, errorText } from './api';
import { Button, ErrorNotice, Field, Modal, SuccessNotice } from './components';

export default function ConversationExport({conversationId}: {conversationId:string}) {
  const [open,setOpen] = useState(false);
  const [busy,setBusy] = useState(false);
  const [copied,setCopied] = useState(false);
  const [error,setError] = useState('');
  const [manualCopy,setManualCopy] = useState<string|null>(null);
  const route = `/conversations/${encodeURIComponent(conversationId)}/export`;

  const copy = async () => {
    setBusy(true);
    setError('');
    setCopied(false);
    try {
      const markdown = manualCopy ?? (await api<{markdown:string}>(route + '?format=markdown')).markdown;
      try {
        await navigator.clipboard.writeText(markdown);
        setCopied(true);
        setManualCopy(null);
      } catch {
        setManualCopy(markdown);
        setError('浏览器未能访问剪贴板，请选中下方内容复制。');
      }
    } catch (cause) {
      setError(errorText(cause));
    } finally {
      setBusy(false);
    }
  };

  return <>
    <button type="button" className="icon-button" title="导出会话" aria-label="导出会话" onClick={() => {
      setError('');
      setCopied(false);
      setManualCopy(null);
      setOpen(true);
    }}><Download size={17}/></button>
    {open && <Modal title="导出会话" onClose={() => { if (!busy) setOpen(false); }}>
      <div className="modal-body">
        <p>复制全部消息、可访问的来源及工具和审批摘要。ZIP 同时包含可下载的附件原件和 JSON 记录。</p>
        <ErrorNotice message={error}/>
        <SuccessNotice message={copied ? '会话 Markdown 已复制。' : ''}/>
        {manualCopy !== null && <Field label="会话 Markdown"><textarea rows={8} readOnly value={manualCopy} onFocus={event => event.currentTarget.select()}/></Field>}
      </div>
      <footer className="modal-footer">
        <Button variant="secondary" busy={busy} onClick={() => void copy()}><Copy size={15}/>复制 Markdown</Button>
        <a className="button primary" href={apiURL(route)} download><Download size={15}/>下载 ZIP</a>
      </footer>
    </Modal>}
  </>;
}
