import { useEffect, useRef, useState } from 'react';
import { api, errorText } from './api';
import type { Message } from './types';

export const chatHistoryLimit = 40;
export interface ChatHistoryPage {
  messages: Message[];
  before: string | null;
  after: string | null;
  latest_run_id: string | null;
}
type HistoryCursor = { before: string } | { after: string } | { around: string };
const emptyHistory: ChatHistoryPage = { messages: [], before: null, after: null, latest_run_id: null };

export async function readChatHistory(conversationId: string, cursor?: HistoryCursor, signal?: AbortSignal): Promise<ChatHistoryPage> {
  const query = new URLSearchParams({ limit: String(chatHistoryLimit), ...cursor });
  const result = await api<ChatHistoryPage | Message[]>(`/conversations/${encodeURIComponent(conversationId)}/messages?${query}`, { signal });
  if (Array.isArray(result)) {
    return { ...emptyHistory, messages: result, latest_run_id: result.slice().reverse().find(message => message.run_id)?.run_id ?? null };
  }
  return result;
}

export function reconcileChatHistory(page: ChatHistoryPage, latest: ChatHistoryPage): ChatHistoryPage {
  const authoritative = new Map(latest.messages.map(item => [item.id, item]));
  if (page.after) return { ...page, messages: page.messages.map(message => authoritative.get(message.id) ?? message) };

  const seam = page.messages.findIndex(message => authoritative.has(message.id));
  const prefix = seam > 0 ? page.messages.slice(0, seam) : [];
  // Keep a just-accepted message if a concurrent read began before it was saved.
  const recent = latest.messages.at(-1)?.created_at;
  const acceptedAfterRead = page.messages.filter((message, index) =>
    index >= Math.max(0, seam) && !authoritative.has(message.id) && (!recent || message.created_at >= recent));
  return { ...latest, before: prefix.length ? page.before : latest.before, messages: [...prefix, ...latest.messages, ...acceptedAfterRead] };
}

export function useChatHistory(conversationId: string) {
  const [page, setPage] = useState<ChatHistoryPage>(emptyHistory);
  const [reading, setReading] = useState(false);
  const [error, setError] = useState('');
  const request = useRef<AbortController | null>(null);
  const keepScroll = useRef<{ id: string; top: number } | null>(null);
  useEffect(() => () => request.current?.abort(), [conversationId]);

  const read = async (direction: 'older' | 'newer' | 'latest', onRead?: () => void) => {
    if (request.current) return;
    const cursor = direction === 'older' && page.before ? { before: page.before }
      : direction === 'newer' && page.after ? { after: page.after } : undefined;
    if (direction !== 'latest' && !cursor) return;
    const controller = new AbortController();
    request.current = controller;
    setReading(true);
    setError('');
    try {
      const incoming = await readChatHistory(conversationId, cursor, controller.signal);
      if (controller.signal.aborted) return;
      onRead?.();
      setPage(current => {
        if (direction === 'latest') return reconcileChatHistory({ ...current, after: null, messages: current.after ? [] : current.messages }, incoming);
        if (direction === 'older' && current.before === page.before) {
          return { ...current, before: incoming.before, messages: [...incoming.messages, ...current.messages] };
        }
        if (direction === 'newer' && current.after === page.after) {
          return { ...current, after: incoming.after, messages: [...current.messages, ...incoming.messages] };
        }
        return current;
      });
    } catch (cause) {
      if (!controller.signal.aborted) setError(errorText(cause));
    } finally {
      if (request.current === controller) request.current = null;
      if (!controller.signal.aborted) setReading(false);
    }
  };

  const addAccepted = (message: Message, follow = true) => setPage(current => {
    if (current.messages.some(item => item.id === message.id)) return current;
    if (current.after) return follow ? { ...current, messages: [message], before: message.id, after: null } : current;
    return { ...current, messages: [...current.messages, message] };
  });
  return { page, setPage, reading, error, keepScroll, read, addAccepted,
    reconcileLatest: (latest: ChatHistoryPage) => setPage(current => reconcileChatHistory(current, latest)) };
}
