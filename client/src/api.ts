import type { Message } from './types';

type RuntimeImportMeta = ImportMeta & { env?: Record<string, string | undefined> };
const clientEnv = (import.meta as RuntimeImportMeta).env;
// Vite's asset base and the API share the reverse-proxy mount. A build without
// an explicit API override must still work under /project/kaiwu/.
export const mountedApiBase = (base: unknown): string => {
  const path = typeof base === 'string' ? base.trim().replace(/^\/+|\/+$/g, '') : '';
  return path && !/[:?#]/.test(path) ? `/${path}/api/v1` : '/api/v1';
};
const DEFAULT_API_BASE = mountedApiBase(clientEnv?.BASE_URL);

/** Keep API paths usable when the client is mounted behind a reverse-proxy prefix. */
export const normalizeApiBase = (value: unknown): string => {
  if (typeof value !== 'string' || !value.trim()) return DEFAULT_API_BASE;
  const raw = value.trim();
  if (/^[a-z][a-z\d+.-]*:\/\//i.test(raw)) return raw.replace(/\/+$/, '');
  const path = raw.startsWith('/') ? raw : `/${raw}`;
  return path.replace(/\/+$/, '') || DEFAULT_API_BASE;
};

export const API_BASE = normalizeApiBase(clientEnv?.VITE_API_BASE);

/** Resolve a route returned by the API against the configured API prefix. */
export const apiURL = (path: string): string => {
  if (!path) return API_BASE;
  if (/^[a-z][a-z\d+.-]*:\/\//i.test(path) || path.startsWith('//')) return path;
  const route = path.startsWith('/') ? path : `/${path}`;
  if (route === API_BASE || route.startsWith(`${API_BASE}/`)) return route;
  const canonical = '/api/v1';
  if (route === canonical || route.startsWith(`${canonical}/`)) return `${API_BASE}${route.slice(canonical.length)}`;
  return `${API_BASE}${route}`;
};

// Plain HTTP pages opened through an EasyConnect IP are not secure contexts,
// so older browsers may not expose crypto.randomUUID there. Keep idempotency
// requests usable without weakening the server-side authorization checks.
export const clientRequestId = () => {
  const randomUUID = globalThis.crypto?.randomUUID;
  if (typeof randomUUID === 'function') return randomUUID.call(globalThis.crypto);
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
};

export class ApiError extends Error {
  constructor(message: string, public status: number, public code: string) { super(message); this.name = 'ApiError'; }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !headers.has('Content-Type') && !(init.body instanceof FormData)) headers.set('Content-Type', 'application/json');
  headers.set('Accept', 'application/json');
  let response: Response;
  try { response = await fetch(apiURL(path), { ...init, headers, credentials: 'include' }); }
  catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new ApiError('暂时无法连接服务，请检查网络后重试。', 0, 'NETWORK_ERROR');
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const message = body.error?.message || (typeof body.detail === 'string' ? body.detail : undefined) || `请求未完成（${response.status}），请重试。`;
    if (response.status === 401 && !init.signal?.aborted && path !== '/auth/login' && path !== '/auth/me') window.dispatchEvent(new Event('session-expired'));
    throw new ApiError(message, response.status, body.error?.code || 'REQUEST_FAILED');
  }
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  return text ? JSON.parse(text) as T : undefined as T;
}

export const post = <T>(path: string, body?: unknown, signal?: AbortSignal) => api<T>(path, { method: 'POST', ...(signal ? { signal } : {}), ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
export const patch = <T>(path: string, body: unknown) => api<T>(path, { method: 'PATCH', body: JSON.stringify(body) });
export const chatMessagePayload = (input: { requestId: string; text: string; fileIds: string[]; skillIds: string[]; trial: boolean; autoModelRouting: boolean }) => ({
  client_request_id: input.requestId,
  text: input.text,
  file_ids: input.fileIds,
  skill_ids: input.skillIds,
  auto_route: !input.trial && input.autoModelRouting,
});
export const sendChatMessage = (conversationId: string, payload: ReturnType<typeof chatMessagePayload> | { client_request_id: string; recovery_run_id: string }, signal?: AbortSignal) =>
  post<{ message: Message; run_id: string }>(`/conversations/${encodeURIComponent(conversationId)}/messages`, payload, signal);
export const remove = (path: string) => api<void>(path, { method: 'DELETE' });
export const errorText = (error: unknown) => error instanceof Error ? error.message : '操作未完成，请重试。';
export const fileURL = (id: string) => apiURL(`/files/${encodeURIComponent(id)}/content`);
export const sizeText = (bytes: number) => bytes < 1024 ? `${bytes} B` : bytes < 1048576 ? `${(bytes / 1024).toFixed(1)} KB` : `${(bytes / 1048576).toFixed(1)} MB`;
export const timeText = (date: string) => new Date(date).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
