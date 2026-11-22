import { useEffect, useState } from 'react';
import { api, errorText, timeText } from './api';
import { Button, ErrorNotice, Modal } from './components';
import type { Message, StoredFile } from './types';

export default function ComposerReferences({ messages, fileIds, onMessage, onFile, onClose }: {
  messages: Message[]; fileIds: string[]; onMessage: (message: Message) => void; onFile: (file: StoredFile) => void; onClose: () => void;
}) {
  const [files, setFiles] = useState<StoredFile[]>([]);
  const [query, setQuery] = useState('');
  const [error, setError] = useState('');
  const idsKey = fileIds.join(',');
  useEffect(() => {
    const controller = new AbortController();
    const ids = idsKey.split(',').filter(Boolean);
    void Promise.allSettled(ids.map(id => api<StoredFile>(`/files/${encodeURIComponent(id)}`, { signal: controller.signal })))
      .then(readings => {
        if (controller.signal.aborted) return;
        setFiles(readings.flatMap(result => result.status === 'fulfilled' ? [result.value] : []));
        const failed = readings.find(result => result.status === 'rejected');
        if (failed?.status === 'rejected' && readings.every(result => result.status === 'rejected')) setError(errorText(failed.reason));
      });
    return () => controller.abort();
  }, [idsKey]);
  const filter = query.trim().toLocaleLowerCase();
  const foundFiles = files.filter(file => file.name.toLocaleLowerCase().includes(filter));
  const foundMessages = messages.filter(message => message.text.trim() && message.text.toLocaleLowerCase().includes(filter)).slice().reverse().slice(0, 30);
  return <Modal title="引用本会话内容" onClose={onClose}>
    <div className="modal-body composer-references">
      <input aria-label="搜索已加载的会话内容" placeholder="搜索文件名或消息" value={query} onChange={event => setQuery(event.target.value)} autoFocus/>
      <ErrorNotice message={error}/>
      {!!foundFiles.length && <section><h3>文件</h3>{foundFiles.map(file => <Button key={file.id} variant="ghost" onClick={() => onFile(file)}>{file.name}</Button>)}</section>}
      {!!foundMessages.length && <section><h3>消息</h3>{foundMessages.map(message => <Button key={message.id} variant="ghost" onClick={() => onMessage(message)}>
        <span>{message.text.trim().slice(0, 140)}{message.text.trim().length > 140 ? '…' : ''}</span><small>{timeText(message.created_at)}</small>
      </Button>)}</section>}
      {!foundFiles.length && !foundMessages.length && <p>当前加载的消息里没有匹配内容。</p>}
    </div>
  </Modal>;
}
