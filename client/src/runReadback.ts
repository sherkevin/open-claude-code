import { ApiError } from './api';
import type { Run } from './types';

const delays = [150, 500, 1250];

export function transientReadError(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 0 || error.status === 408 || error.status === 429 || error.status >= 500);
}

export function waitForRead(delay: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) { reject(signal.reason); return; }
    const abort = () => { clearTimeout(timer); reject(signal.reason); };
    const timer = window.setTimeout(() => { signal.removeEventListener('abort', abort); resolve(); }, delay);
    signal.addEventListener('abort', abort, { once: true });
  });
}

export async function retryRead<T>(read: () => Promise<T>, signal: AbortSignal): Promise<T> {
  for (let attempt = 0; ; attempt += 1) {
    try { return await read(); }
    catch (error) {
      if (signal.aborted || attempt === delays.length || !transientReadError(error)) throw error;
      await waitForRead(delays[attempt], signal);
    }
  }
}

export function pendingRun(id: string, conversationId: string): Run {
  return { id, conversation_id: conversationId, status: 'queued', assistant_message_id: null,
    error: null, usage: null, approvals: [], created_at: new Date().toISOString(), completed_at: null };
}
