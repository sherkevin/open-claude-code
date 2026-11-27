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
const logoutKey = (owner: string) => composerOwnerPrefix(owner) + 'engineering-logout';
function accountStamp(owner: string) {
  try { return localStorage.getItem(logoutKey(owner)) ?? ''; } catch { return ''; }
}

async function access(owner: string, conversation: string, stamp: string, op?: Change,
  repair?: { base: string; draft: Draft }) {
  const db = await open();
  return new Promise<{ draft: Draft; changed: boolean }>((resolve, reject) => {
    const tx = db.transaction(storeName, op ? 'readwrite' : 'readonly');
    const store = tx.objectStore(storeName);
    const key = [owner, conversation];
    const get = store.get(key);
    let draft: Draft;
    let changed = false;
    get.onsuccess = () => {
      if (accountStamp(owner) !== stamp) { draft = empty(); return; }
      const previous: Draft = get.result && (get.result.stamp ?? '') === stamp ? get.result : empty();
      // An earlier edit may have survived only in memory. Preserve it only if
      // the durable version still matches the one read before storage failed.
      if (repair && repair.base !== previous.revision) { draft = previous; return; }
      const source = repair ? { ...previous, text: repair.draft.text, revision: repair.draft.revision } : previous;
      draft = op ? changeCodexDraft(source, op) : source;
      changed = source !== draft;
      if (changed) store.put({ ...draft, stamp }, key);
    };
    tx.oncomplete = () => resolve({ draft, changed });
    tx.onabort = tx.onerror = () => reject(tx.error || new Error('浏览器草稿存储不可用'));
  });
}

const signal = 'kaiwu:engineering-draft-change';
function broadcast(scope: string) {
  try { localStorage.setItem(signal, JSON.stringify([scope, clientRequestId()])); } catch { /* Reads on focus still synchronize. */ }
}
type Snapshot = Draft & { ready: boolean; memoryOnly: boolean; conflict: boolean; revoked: boolean; submitFailed: boolean };
function createStore(owner: string, conversation: string) {
  const scope = composerScope(owner, conversation);
  const stamp = accountStamp(owner);
  const listeners = new Set<() => void>();
  let state: Snapshot = { ...empty(), ready: false, memoryOnly: false, conflict: false, revoked: false, submitFailed: false };
  let fallback = empty();
  let durableRevision: string | null = null;
  let failedClaim: { optimistic: string; actual: string } | undefined;
  let queue: Promise<unknown> = Promise.resolve();
  let writes = 0;
  let sequence = 0;
  let closed = false;
  const publish = (data: Partial<Snapshot>) => {
    if (closed) return;
    state = { ...state, ...data };
    for (const listener of listeners) listener();
  };
  function dispose() { publish({ ...empty(), ready: false, revoked: true }); closed = true; listeners.clear(); }
  function sessionCurrent() {
    if (!closed && accountStamp(owner) !== stamp) dispose();
    return !closed;
  }
  async function reload() {
    if (!sessionCurrent() || writes || state.memoryOnly) return;
    const generation = ++sequence;
    try {
      const { draft } = await access(owner, conversation, stamp);
      if (generation === sequence) { fallback = draft; durableRevision = draft.revision; publish({ ...draft, ready: true }); }
    } catch {
      if (generation === sequence) publish({ ready: true, memoryOnly: true });
    }
  }
  function mutate(op: Change): Promise<boolean> {
    if (!sessionCurrent() || !state.ready) return Promise.resolve(false);
    const predicted = changeCodexDraft(state, op);
    if (predicted === state) return Promise.resolve(false);
    sequence++;
    writes++;
    publish({ ...predicted, conflict: false, ...(op.kind === 'settle' ? {} : { submitFailed: false }) });
    const task = queue.then(async () => {
      let changed = false;
      let submitFailed = false;
      const change = op.kind !== 'settle' && failedClaim?.optimistic === op.base ? { ...op, base: failedClaim.actual } : op;
      if (sessionCurrent()) {
        let useMemory = state.memoryOnly && durableRevision === null;
        if (!useMemory) {
          try {
            const repair = state.memoryOnly && durableRevision !== null ? { base: durableRevision, draft: fallback } : undefined;
            const result = await access(owner, conversation, stamp, change, repair);
            fallback = result.draft;
            durableRevision = result.draft.revision;
            changed = result.changed;
            publish({ memoryOnly: false });
            if (changed) broadcast(scope);
          } catch {
            if (change.kind === 'submit' && durableRevision !== null) {
              // No POST may escape while the disk still describes this text as unsent.
              submitFailed = true;
              failedClaim = { optimistic: change.revision, actual: fallback.revision };
              publish({ submitFailed: true });
            } else {
              useMemory = true;
              publish({ memoryOnly: true });
            }
          }
        }
        if (useMemory) {
          const next = changeCodexDraft(fallback, change);
          changed = next !== fallback;
          fallback = next;
        }
      }
      writes--;
      if (!writes) publish({ ...fallback, conflict: !changed && !submitFailed && op.kind !== 'settle' });
      return sessionCurrent() && changed;
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
    dispose, sessionCurrent,
  };
}
const stores = new Map<string, ReturnType<typeof createStore>>();

export function clearCodexDrafts(owner: string) {
  const nextStamp = clientRequestId();
  try { localStorage.setItem(logoutKey(owner), nextStamp); } catch { /* Transaction still clears persisted records. */ }
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
        // A newly authorized page may already have written under the new login.
        if (cursor.value.stamp !== nextStamp) cursor.delete();
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
    if (!found || !found.sessionCurrent()) { found = createStore(owner, conversation); stores.set(key, found); }
    return found;
  });
  const state = useSyncExternalStore(store.subscribe, store.read, store.read);
  useEffect(() => {
    if (!authorized) return;
    void store.reload();
    const onStorage = (event: StorageEvent) => {
      if (event.key === logoutKey(owner)) { store.sessionCurrent(); return; }
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
