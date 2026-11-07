export const API_BASE = '/api/v1';

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
  if (init.body && !(init.body instanceof FormData)) headers.set('Content-Type', 'application/json');
  headers.set('Accept', 'application/json');
  let response: Response;
  try { response = await fetch(`${API_BASE}${path}`, { ...init, headers, credentials: 'include' }); }
  catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new ApiError('暂时无法连接服务，请检查网络后重试。', 0, 'NETWORK_ERROR');
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const message = body.error?.message || (typeof body.detail === 'string' ? body.detail : undefined) || `请求未完成（${response.status}），请重试。`;
    if (response.status === 401 && path !== '/auth/login' && path !== '/auth/me') window.dispatchEvent(new Event('session-expired'));
    throw new ApiError(message, response.status, body.error?.code || 'REQUEST_FAILED');
  }
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  return text ? JSON.parse(text) as T : undefined as T;
}

export const post = <T>(path: string, body?: unknown) => api<T>(path, { method: 'POST', ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
export const patch = <T>(path: string, body: unknown) => api<T>(path, { method: 'PATCH', body: JSON.stringify(body) });
export const remove = (path: string) => api<void>(path, { method: 'DELETE' });
export const errorText = (error: unknown) => error instanceof Error ? error.message : '操作未完成，请重试。';
export const fileURL = (id: string) => `${API_BASE}/files/${encodeURIComponent(id)}/content`;
export const sizeText = (bytes: number) => bytes < 1024 ? `${bytes} B` : bytes < 1048576 ? `${(bytes / 1024).toFixed(1)} KB` : `${(bytes / 1048576).toFixed(1)} MB`;
export const timeText = (date: string) => new Date(date).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
