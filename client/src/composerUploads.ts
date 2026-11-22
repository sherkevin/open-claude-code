import { useEffect, useState, useSyncExternalStore } from 'react';
import { ApiError, apiURL, clientRequestId, errorText } from './api';
import { deleteOwnerUploads, deleteUpload, putUpload, readUploads, type StoredUpload } from './composerUploadStorage';
import type { StoredFile } from './types';

export type UploadJob = {
  id: string;
  file: File;
  status: 'queued' | 'uploading' | 'failed';
  progress?: number;
  error?: string;
  storageWarning?: boolean;
};
type Queue = ReturnType<typeof makeQueue>;
const queues = new Map<string, Queue>();

function queueKey(owner: string, conversation: string) {
  return encodeURIComponent(owner) + ':' + encodeURIComponent(conversation);
}

function transfer(id: string, file: File, binary: boolean, signal: AbortSignal, progress: (value?: number) => void) {
  return new Promise<StoredFile>((resolve, reject) => {
    const request = new XMLHttpRequest();
    const cancelled = () => request.abort();
    request.open('POST', apiURL(`${binary ? '/files/binary' : '/files'}?draft=true&upload_id=${encodeURIComponent(id)}`));
    request.withCredentials = true;
    request.setRequestHeader('Accept', 'application/json');
    if (binary) {
      request.setRequestHeader('Content-Type', 'application/octet-stream');
      request.setRequestHeader('X-Upload-Filename', encodeURIComponent(file.name));
    }
    request.upload.onprogress = event => progress(event.lengthComputable ? event.loaded / event.total : undefined);
    request.onloadend = () => signal.removeEventListener('abort', cancelled);
    request.onabort = () => reject(new DOMException('Upload cancelled', 'AbortError'));
    request.onerror = () => reject(new ApiError('连接中断，请重试上传。', 0, 'NETWORK_ERROR'));
    request.onload = () => {
      let body;
      try { body = JSON.parse(request.responseText); } catch { body = null; }
      if (request.status === 401) window.dispatchEvent(new Event('session-expired'));
      if (request.status >= 200 && request.status < 300 && body && typeof body.id === 'string') resolve(body);
      else reject(new ApiError(body?.error?.message || `附件上传未完成（${request.status}），请重试。`, request.status, body?.error?.code || 'UPLOAD_FAILED'));
    };
    if (signal.aborted) return reject(new DOMException('Upload cancelled', 'AbortError'));
    signal.addEventListener('abort', cancelled, { once: true });
    if (binary) request.send(file);
    else {
      const body = new FormData();
      body.append('file', file);
      request.send(body);
    }
  });
}

function makeQueue(owner?: string, scope?: string) {
  let jobs: UploadJob[] = [];
  let restoring = Boolean(owner && scope);
  let hydrated = false;
  let disposed = false;
  let restorationError = '';
  let save: (file: StoredFile, order: number) => void;
  let lastOrder = 0;
  const removed = new Set<string>();
  const listeners = new Set<() => void>();
  const entries = new Map<string, StoredUpload>();
  const active = new Map<string, AbortController>();
  const update = (next: UploadJob[]) => {
    jobs = next;
    for (const listener of listeners) listener();
  };
  const patch = (id: string, change: Partial<UploadJob>) =>
    update(jobs.map(job => job.id === id ? { ...job, ...change } : job));
  const remove = (id: string) => {
    if (restoring) removed.add(id);
    entries.delete(id);
    update(jobs.filter(job => job.id !== id));
    if (scope) void deleteUpload(scope, id).catch(() => {});
  };
  const persist = (entry: StoredUpload) => {
    if (!scope || !owner) return;
    void putUpload(entry).catch(() => patch(entry.id, { storageWarning: true }));
  };
  const start = () => {
    while (active.size < 2) {
      const next = jobs.find(job => job.status === 'queued');
      if (!next) break;
      const entry = entries.get(next.id);
      if (!entry) break;
      const controller = new AbortController();
      active.set(next.id, controller);
      patch(next.id, { status: 'uploading', progress: 0, error: undefined });
      void transfer(next.id, next.file, entry.binary, controller.signal, progress => patch(next.id, { progress }))
        .then(saved => {
          if (!entries.has(next.id)) return;
          save(saved, entry.order ?? entry.receivedAt * 1000);
          remove(next.id);
        })
        .catch(error => {
          if (entries.has(next.id)) {
            entry.error = errorText(error);
            patch(next.id, { status: 'failed', error: entry.error });
            persist(entry);
          }
        })
        .finally(() => {
          active.delete(next.id);
          start();
        });
    }
  };
  return {
    subscribe(listener: () => void) {
      listeners.add(listener);
      return () => { listeners.delete(listener); };
    },
    read: () => jobs,
    restoring: () => restoring,
    restorationError: () => restorationError,
    hydrate(onSaved: (file: StoredFile, order: number) => void) {
      save = onSaved;
      if (hydrated || !scope || !owner) return;
      hydrated = true;
      void readUploads(scope).then(stored => {
        if (disposed) return;
        const recovered: UploadJob[] = [];
        for (const item of stored.sort((a, b) => (a.order ?? a.receivedAt * 1000) - (b.order ?? b.receivedAt * 1000))) {
          if (item.owner !== owner || entries.has(item.id) || removed.has(item.id)) continue;
          entries.set(item.id, item);
          lastOrder = Math.max(lastOrder, item.order ?? item.receivedAt * 1000);
          recovered.push({ id:item.id, file:item.file, status:'failed', error:item.error || '上传随页面关闭而中断' });
        }
        restoring = false;
        removed.clear();
        update([...recovered, ...jobs]);
      }).catch(() => {
        if (disposed) return;
        restoring = false;
        removed.clear();
        restorationError = '此浏览器无法读取未完成上传，刷新前请等待附件上传完成。';
        update([...jobs]);
      });
    },
    add(files: File[], binary: boolean, onSaved: (file: StoredFile, order: number) => void) {
      save = onSaved;
      const next = files.map(file => ({ id: clientRequestId(), file, status: 'queued' as const }));
      update([...jobs, ...next]);
      for (const job of next) {
        const receivedAt = Date.now();
        lastOrder = Math.max(lastOrder + 1, receivedAt * 1000);
        const entry = { owner: owner || '', scope: scope || '', id: job.id, file: job.file, binary, receivedAt, order: lastOrder };
        entries.set(job.id, entry);
        persist(entry);
      }
      start();
    },
    cancel(id: string) {
      remove(id);
      active.get(id)?.abort();
      start();
    },
    retry(id: string) {
      if (!jobs.some(job => job.id === id && job.status === 'failed')) return;
      patch(id, { status: 'queued', error: undefined });
      start();
    },
    dispose() {
      disposed = true;
      entries.clear();
      for (const controller of active.values()) controller.abort();
      update([]);
    },
  };
}

export function clearComposerUploads(owner: string) {
  const prefix = encodeURIComponent(owner) + ':';
  for (const [key, queue] of queues) {
    if (!key.startsWith(prefix)) continue;
    queue.dispose();
    queues.delete(key);
  }
  void deleteOwnerUploads(owner).catch(() => {});
}

function browserScope(key: string) {
  try {
    const storageKey = 'kaiwu:upload-tab';
    const token = sessionStorage.getItem(storageKey) || clientRequestId();
    sessionStorage.setItem(storageKey, token);
    return `${key}:${token}`;
  } catch {
    return undefined;
  }
}

export function useComposerUploads(owner: string | undefined, conversation: string, onSaved: (file: StoredFile, order: number) => void) {
  const [queue] = useState(() => {
    if (!owner) return makeQueue();
    const key = queueKey(owner, conversation);
    const existing = queues.get(key);
    if (existing) return existing;
    const created = makeQueue(owner, browserScope(key));
    queues.set(key, created);
    return created;
  });
  useEffect(() => { queue.hydrate(onSaved); }, [queue, onSaved]);
  const jobs = useSyncExternalStore(queue.subscribe, queue.read, queue.read);
  return { queue, jobs, restoring: queue.restoring(), restorationError: queue.restorationError(), uploading: jobs.some(job => job.status !== 'failed') };
}
