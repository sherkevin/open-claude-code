import { afterEach, describe, expect, it, vi } from 'vitest';
import { apiURL, clientRequestId, mountedApiBase, normalizeApiBase } from './api';

afterEach(() => { vi.unstubAllGlobals(); });

describe('client request identifiers', () => {
  it('uses the browser UUID implementation when available', () => {
    vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'browser-uuid') });
    expect(clientRequestId()).toBe('browser-uuid');
  });

  it('falls back on non-secure HTTP pages without crypto.randomUUID', () => {
    vi.stubGlobal('crypto', undefined);
    const id = clientRequestId();
    expect(id).toMatch(/^[a-z0-9]+-[a-z0-9]+-[a-z0-9]+$/);
    expect(id.length).toBeGreaterThanOrEqual(8);
  });
});

describe('reverse-proxy API prefixes', () => {
  it('derives the API mount from the asset base when the build has no override', () => {
    expect(mountedApiBase('/project/kaiwu/')).toBe('/project/kaiwu/api/v1');
    expect(mountedApiBase('/zhixing-next/')).toBe('/zhixing-next/api/v1');
    expect(mountedApiBase('/')).toBe('/api/v1');
    expect(mountedApiBase(undefined)).toBe('/api/v1');
    expect(mountedApiBase('https://cdn.test/assets/')).toBe('/api/v1');
  });
  it('keeps the default API route and normalizes a mounted prefix', () => {
    expect(normalizeApiBase(undefined)).toBe('/api/v1');
    expect(normalizeApiBase('/zhixing/api/v1/')).toBe('/zhixing/api/v1');
    expect(normalizeApiBase('https://example.test/api/v1/')).toBe('https://example.test/api/v1');
  });

  it('does not duplicate the canonical prefix when the server returns source links', () => {
    expect(apiURL('/api/v1/sources/example')).toBe('/api/v1/sources/example');
    expect(apiURL('/knowledge-documents/example/content')).toBe('/api/v1/knowledge-documents/example/content');
  });
});
