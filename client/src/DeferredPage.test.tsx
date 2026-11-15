import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { deferredPage, retryablePageLoader } from './DeferredPage';

describe('on-demand page loading', () => {
  it('does not load an unopened page and reuses a successful or pending request', async () => {
    let finish!: (value: string) => void;
    const load = vi.fn(() => new Promise<string>(resolve => { finish = resolve; }));
    const page = retryablePageLoader(load);
    expect(load).not.toHaveBeenCalled();
    const first = page(); const second = page();
    expect(first).toBe(second);
    await Promise.resolve(); expect(load).toHaveBeenCalledTimes(1);
    finish('page');
    await expect(first).resolves.toBe('page');
    await expect(page()).resolves.toBe('page');
    expect(load).toHaveBeenCalledTimes(1);
  });
  it('permits a new request after a download failure instead of caching the failure', async () => {
    const load = vi.fn().mockRejectedValueOnce(new Error('network unavailable')).mockResolvedValueOnce('page');
    const page = retryablePageLoader(load);
    await expect(page()).rejects.toThrow('network unavailable');
    await expect(page()).resolves.toBe('page');
    expect(load).toHaveBeenCalledTimes(2);
  });
  it('shows a local loading state while the requested page is unresolved', () => {
    const Page = deferredPage(() => new Promise<{ default: () => JSX.Element }>(() => {}));
    const html = renderToStaticMarkup(<><nav>其他页面</nav><Page/></>);
    expect(html).toContain('其他页面');
    expect(html).toContain('role="status"');
    expect(html).toContain('正在打开页面');
  });
});
