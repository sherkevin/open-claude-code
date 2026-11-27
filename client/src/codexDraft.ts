import { useEffect, useState, useSyncExternalStore } from 'react';
import { browserDraftDatabase, composerOwnerPrefix, composerScope } from './browserDraftStorage';
import { clientRequestId } from './api';

export type DraftAttempt = { kind: 'turn' | 'steer'; id: string; turn?: string; text: string };
type Draft = { revision: string; text: string; attempts: DraftAttempt[] };
const empty = (): Draft => ({ revision: '', text: '', attempts: [] });
type Change = { kind: 'edit'; base: string; revision: string; text: string }
  | { kind: 'submit'; base: string; revision: string; attempt: DraftAttempt }
  | { kind: 'settle'; id: string };

export function changeCodexDraft(current: Draft, op: Change): Draft {
  if (op.kind === 'settle') {
    return current.attempts.some(row => row.id === op.id)
      ? { ...current, attempts: current.attempts.filter(row => row.id !== op.id) } : current;
  }
  if (op.base !== current.revision) return current;
  if (op.kind === 'edit') return { ...current, revision: op.revision, text: op.text };
  if (!current.text.trim() || current.text !== op.attempt.text || current.attempts.some(row => row.id === op.attempt.id || (row.kind === 'turn' && op.attempt.kind === 'turn'))) return current;
  return { revision: op.revision, text: '', attempts: [...current.attempts, op.attempt] };
}

const storeName = 'drafts';
const open = browserDraftDatabase('kaiwu-engineering-drafts', db => { db.createObjectStore(storeName); }, '无法打开浏览器草稿存储');

// A single read/write transaction compares the editor's base revision with other tabs.
async function access(owner: string, conversation: string, op?: Change) {
  const db = await open();
  return new Promise<{ draft: Draft; changed: boolean }>((resolve, reject) => {
    const tx = db.transaction(storeName, op ? 'readwrite' : 'readonly');
    const store = tx.objectStore(storeName);
    const key = [owner, conversation];
    const get = store.get(key);
    let draft: Draft;
    let changed = false;
    get.onsuccess = () => {
      const previous: Draft = get.result ?? empty();
      draft = op ? changeCodexDraft(previous, op) : previous;
      changed = previous !== draft;
      if (changed) store.put(draft, key);
    };
    tx.oncomplete = () => resolve({ draft, changed });
    tx.onabort = tx.onerror = () => reject(tx.error || new Error('浏览器草稿存储不可用'));
  });
}

const signal = 'kaiwu:engineering-draft-change';
function broadcast(scope: string) {
  try { localStorage.setItem(signal, JSON.stringify([scope, clientRequestId()])); } catch { /* Reads on focus still synchronize. */ }
}
type Snapshot = Draft & { ready: boolean; memoryOnly: boolean; conflict: boolean };
function createStore(owner: string, conversation: string) {
  const scope = composerScope(owner, conversation);
  const listeners = new Set<() => void>();
  let state: Snapshot = { ...empty(), ready: false, memoryOnly: false, conflict: false };
  let fallback = empty();
  let queue: Promise<unknown> = Promise.resolve();
  let writes = 0;
  let sequence = 0;
  let closed = false;
  const publish = (data: Partial<Snapshot>) => {
    if (closed) return;
    state = { ...state, ...data };
    for (const listener of listeners) listener();
  };
  async function reload() {
    if (closed || writes || state.memoryOnly) return;
    const generation = ++sequence;
    try {
      const { draft } = await access(owner, conversation);
      if (generation === sequence) { fallback = draft; publish({ ...draft, ready: true }); }
    } catch {
      if (generation === sequence) publish({ ready: true, memoryOnly: true });
    }
  }
  function mutate(op: Change): Promise<boolean> {
    if (closed || !state.ready) return Promise.resolve(false);
    const predicted = changeCodexDraft(state, op);
    if (predicted === state) return Promise.resolve(false);
    sequence++;
    writes++;
    publish({ ...predicted, conflict: false });
    const task = queue.then(async () => {
      let changed = false;
      if (!closed) {
        if (!state.memoryOnly) {
          try {
            const result = await access(owner, conversation, op);
            fallback = result.draft;
            changed = result.changed;
            if (changed) broadcast(scope);
          } catch { publish({ memoryOnly: true }); }
        }
        if (state.memoryOnly) {
          const next = changeCodexDraft(fallback, op);
          changed = next !== fallback;
          fallback = next;
        }
      }
      writes--;
      if (!writes) publish({ ...fallback, conflict: !changed && op.kind !== 'settle' });
      return !closed && changed;
    });
    queue = task;
    return task;
  }
  return {
    scope, owner, subscribe: (callback: () => void) => { listeners.add(callback); return () => { listeners.delete(callback); }; },
    read: () => state, reload,
    edit: (base: string, text: string) => mutate({ kind: 'edit', base, revision: clientRequestId(), text }),
    submit: (base: string, attempt: DraftAttempt) => mutate({ kind: 'submit', base, revision: clientRequestId(), attempt }),
    settle: (id: string) => mutate({ kind: 'settle', id }),
    dispose() { publish({ ...empty(), ready: false }); closed = true; listeners.clear(); },
  };
}
const stores = new Map<string, ReturnType<typeof createStore>>();

export function clearCodexDrafts(owner: string) {
  for (const [key, store] of stores) {
    if (store.owner === owner) { store.dispose(); stores.delete(key); }
  }
  const remove = async () => {
    const db = await open();
    const tx = db.transaction(storeName, 'readwrite');
    const store = tx.objectStore(storeName);
    const request = store.openCursor(IDBKeyRange.bound([owner], [owner, []]));
    request.onsuccess = () => {
      const cursor = request.result;
      if (cursor) {
        // Keep a revision barrier so an old tab cannot restore the cleared text.
        cursor.update({ ...empty(), revision: clientRequestId() });
        cursor.continue();
      }
    };
    tx.oncomplete = () => broadcast(composerOwnerPrefix(owner));
  };
  void remove().catch(() => { /* Memory-only data was cleared above. */ });
}

export function useCodexDraft(owner: string, conversation: string, authorized: boolean) {
  const [store] = useState(() => {
    const key = composerScope(owner, conversation);
    let found = stores.get(key);
    if (!found) { found = createStore(owner, conversation); stores.set(key, found); }
    return found;
  });
  const state = useSyncExternalStore(store.subscribe, store.read, store.read);
  useEffect(() => {
    if (!authorized) return;
    void store.reload();
    const onStorage = (event: StorageEvent) => {
      if (event.key !== signal || !event.newValue) return;
      try {
        const [scope] = JSON.parse(event.newValue);
        if (scope === store.scope || scope === composerOwnerPrefix(owner)) void store.reload();
      } catch { /* Ignore unrelated or old notices. */ }
    };
    const onFocus = () => { void store.reload(); };
    window.addEventListener('storage', onStorage);
    window.addEventListener('focus', onFocus);
    return () => { window.removeEventListener('storage', onStorage); window.removeEventListener('focus', onFocus); };
  }, [store, owner, authorized]);
  return { state, store };
}
