import { useState, useSyncExternalStore } from 'react';
import type { StoredFile } from './types';

const prefix = 'kaiwu:composer:';
export type Submission = { signature: string; id: string; queued: boolean };
export type DraftAttachment = StoredFile & { selectionOrder?: number };
type Draft = { text: string; files: DraftAttachment[]; skills: string[]; preview?: StoredFile; submission?: Submission };
type DraftValue<T> = T | ((previous: T) => T);
type DraftStore = ReturnType<typeof makeStore>;
const drafts = new Map<string, DraftStore>();

function ownerPrefix(owner: string) {
  return prefix + encodeURIComponent(owner) + ':';
}

function readDraft(key: string | null, text: string, initialSubmission?: Submission): Draft {
  const empty = { text, files: [], skills: [], submission: initialSubmission };
  if (!key) return empty;
  try {
    const raw = sessionStorage.getItem(key);
    if (!raw) return empty;
    const value = JSON.parse(raw);
    const files = Array.isArray(value?.files) ? value.files.filter((file: StoredFile) =>
      file && typeof file.id === 'string' && typeof file.name === 'string' &&
      Number.isSafeInteger(file.size) && typeof file.mime_type === 'string') : [];
    const preview = value?.preview;
    const submission = value?.submission;
    return { text: typeof value?.text === 'string' ? value.text : text, files,
      skills: Array.isArray(value?.skills) ? value.skills.filter((id: unknown) => typeof id === 'string').slice(0, 5) : [],
      ...(submission && typeof submission.id === 'string' && typeof submission.signature === 'string'
        && typeof submission.queued === 'boolean' ? { submission } : {}),
      ...(preview && typeof preview.id === 'string' && typeof preview.name === 'string' &&
        Number.isSafeInteger(preview.size) && typeof preview.mime_type === 'string' ? { preview } : {}) };
  } catch {
    return empty;
  }
}

export function clearComposerDrafts(owner: string) {
  const start = ownerPrefix(owner);
  for (const [key, store] of drafts) {
    if (key.startsWith(start)) {
      store.dispose();
      drafts.delete(key);
    }
  }
  try {
    for (const item of Object.keys(sessionStorage)) {
      if (item.startsWith(start)) sessionStorage.removeItem(item);
    }
  } catch {
    // A browser that disables storage still supports an in-memory draft.
  }
}

function makeStore(key: string | null, initial: string, initialSubmission?: Submission) {
  let value = readDraft(key, initial, initialSubmission);
  let disposed = false;
  const callbacks = new Set<() => void>();
  const update = (mutate: (previous: Draft) => Draft) => {
    if (disposed) return;
    const next = mutate(value);
    value = next;
    try {
      if (key) {
        if (next.text || next.files.length || next.skills.length || next.preview || next.submission) sessionStorage.setItem(key, JSON.stringify(next));
        else sessionStorage.removeItem(key);
      }
    } catch {
      // Draft editing keeps working if storage is unavailable or full.
    }
    for (const callback of callbacks) callback();
  };
  return {
    subscribe(callback: () => void) {
      callbacks.add(callback);
      return () => { callbacks.delete(callback); };
    },
    read: () => value,
    setText: (next: DraftValue<string>) => update(previous => ({ ...previous, text: typeof next === 'function' ? next(previous.text) : next })),
    setFiles: (next: DraftValue<DraftAttachment[]>) => update(previous => ({ ...previous, files: typeof next === 'function' ? next(previous.files) : next })),
    setSkills: (next: DraftValue<string[]>) => update(previous => ({ ...previous, skills: typeof next === 'function' ? next(previous.skills) : next })),
    setSubmission: (submission?: Submission) => update(previous => ({ ...previous, submission })),
    finishSubmission(id: string, snapshot: Draft) {
      update(current => {
        if (current.submission?.id !== id) return current;
        const same = current.text === snapshot.text && current.files === snapshot.files && current.skills === snapshot.skills;
        return same ? { ...current, text: '', files: [], skills: [], submission: undefined }
          : { ...current, submission: undefined };
      });
    },
    setPreview: (preview: StoredFile | null) => update(previous => ({ ...previous, preview: preview || undefined })),
    dispose() { disposed = true; callbacks.clear(); },
  };
}

export function useComposerDraft(owner: string | undefined, conversationId: string, initialText: string, initialSubmission?: Submission) {
  const [store] = useState(() => {
    if (!owner) return makeStore(null, initialText, initialSubmission);
    const key = ownerPrefix(owner) + encodeURIComponent(conversationId);
    const existing = drafts.get(key);
    if (existing) return existing;
    const created = makeStore(key, initialText, initialSubmission);
    drafts.set(key, created);
    return created;
  });
  const draft = useSyncExternalStore(store.subscribe, store.read, store.read);
  return { draft, readDraft: store.read, setText: store.setText, setFiles: store.setFiles, setSkills: store.setSkills,
    setPreview: store.setPreview, setSubmission: store.setSubmission, finishSubmission: store.finishSubmission };
}
