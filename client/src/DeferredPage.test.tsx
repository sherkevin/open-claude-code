import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { deferredPage, PageLoadFailure, retryablePageLoader } from './DeferredPage';

afterEach(() => vi.unstubAllGlobals());

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
  it('keeps a full reload explicit and warns about unsaved input', () => {
    const reload=vi.fn();const retry=vi.fn();
    vi.stubGlobal('window',{location:{reload}});
    const html=renderToStaticMarkup(<PageLoadFailure onRetry={retry}/>);
    expect(html).toContain('重新打开');expect(html).toContain('刷新工作台');
    expect(html).toContain('已保存的会话会保留');expect(html).toContain('未保存的输入会丢失');
    expect(reload).not.toHaveBeenCalled();expect(retry).not.toHaveBeenCalled();
    const actions=PageLoadFailure({onRetry:retry}).props.children[1].props.children;
    actions[0].props.onClick();expect(retry).toHaveBeenCalledOnce();expect(reload).not.toHaveBeenCalled();
    actions[1].props.onClick();expect(reload).toHaveBeenCalledOnce();
  });
});
