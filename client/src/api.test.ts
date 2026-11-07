import { afterEach, describe, expect, it, vi } from 'vitest';
import { clientRequestId } from './api';

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
