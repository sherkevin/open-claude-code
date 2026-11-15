import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from './api';
import { ConnectionEditor, ConnectionOnboardingCard, connectionBindings, connectionDiscoveryState, connectionTestError, loadConnectionOnboarding, saveConnectionConfiguration, testConnectionDiscovery } from './ConnectionsPage';
import type { Assistant, Connection } from './types';

const connection: Connection = { id: 'orders', name: '订单服务', transport: 'http', url: 'https://mcp.example/mcp', auth_type: 'none', credential_present: false, enabled: true, status: 'connected', tools: [{ name: 'orders_total', description: '汇总订单', inputSchema: {} }], last_error: null, last_tested_at: '2026-11-14T09:00:00Z', revision: 2, scope: 'private', can_edit: true };
const assistant: Assistant = { id: 'assistant', name: '采购助手', description: '', instructions: '', enabled: true, access: { all_members: false, user_ids: [] }, connections: [{ connection_id: 'orders', allowed_tools: ['orders_total'], auto_approved_tools: [] }], revision: 1 };
const noop = () => {};
const card = (item: Connection = connection, assistants: Assistant[] | null = [], extras: Partial<Parameters<typeof ConnectionOnboardingCard>[0]> = {}) => renderToStaticMarkup(<ConnectionOnboardingCard item={item} assistants={assistants} admin={false} onTest={noop} onTools={noop} onEdit={noop} onDelete={noop} onConfigureAssistant={noop} {...extras}/>);
afterEach(() => vi.unstubAllGlobals());

describe('connection-specific onboarding evidence', () => {
  it('does not mark assistant binding or a business trial complete when discovery succeeds', () => {
    const html = card();
    expect(html).toContain('已发现 1 个工具');
    expect(html).toContain('尚未绑定助手');
    expect(html).toContain('业务结果待验证');
    expect(html.match(/data-state="complete"/g)).toHaveLength(2);
    expect(html).not.toContain('业务验证通过</');
  });
  it('matches binding IDs independently instead of borrowing another connection success', () => {
    expect(connectionBindings(connection, [assistant])).toHaveLength(1);
    expect(connectionBindings({ ...connection, id: 'inventory' }, [assistant])).toEqual([]);
    expect(card({ ...connection, id: 'inventory' }, [assistant])).toContain('尚未绑定助手');
  });
  it('counts each visible assistant once and distinguishes disabled and ungranted bindings', () => {
    const duplicate = { ...assistant, connections: [...assistant.connections, assistant.connections[0]] };
    const disabled = { ...assistant, id: 'off', name: '停用助手', enabled: false };
    const noGrant = { ...assistant, id: 'empty', connections: [{ connection_id: 'orders', allowed_tools: ['deleted-tool'], auto_approved_tools: ['orders_total'] }] };
    const html = card(connection, [duplicate, disabled, noGrant]);
    expect(connectionBindings(connection, [duplicate])?.[0].allowedCount).toBe(1);
    expect(html).toContain('已绑定 3 个助手');
    expect(html).toContain('当前可见助手中，1 个已启用并允许调用已发现工具');
    expect(html).toContain('已停用');
    expect(html).toContain('允许 0 个已发现工具');
    expect(html).toContain('业务结果待验证');
  });
  it('keeps unknown binding separate from an empty authorized assistant list', () => {
    expect(connectionBindings(connection, null)).toBeNull();
    expect(card(connection, null)).toContain('绑定情况未确认');
    expect(card(connection, null)).not.toContain('尚未绑定助手');
    expect(card(connection, [])).toContain('尚未绑定助手');
  });
  it('does not treat empty discovery, stale timestamps or disabled connections as ready', () => {
    expect(connectionDiscoveryState({ ...connection, tools: [] })).toMatchObject({ label: '已连接，未发现工具', complete: false });
    expect(connectionDiscoveryState({ ...connection, status: 'untested' }).complete).toBe(false);
    expect(card({ ...connection, status: 'untested' })).toContain('当前配置待重测');
    expect(connectionDiscoveryState({ ...connection, enabled: false }).complete).toBe(false);
  });
  it('distinguishes remote auth, remote unknown failure and workbench permission failure', () => {
    expect(connectionDiscoveryState({ ...connection, status: 'needs_auth' }).label).toBe('认证或权限未通过');
    expect(connectionDiscoveryState({ ...connection, status: 'failed' }).detail).toContain('具体失败阶段未确认');
    expect(connectionTestError(new ApiError('Forbidden', 403, 'FORBIDDEN'))).toContain('当前账号无权测试');
    expect(connectionTestError(new ApiError('Expired', 401, 'UNAUTHORIZED'))).toContain('工作台登录已失效');
  });
  it('does not retain the completed discovery badge after a fresh request fails or while testing', () => {
    const failed = card(connection, [], { testError: '测试请求未完成' });
    expect(failed).toContain('本次测试未完成');
    expect(failed).not.toContain('badge green');
    expect(failed.match(/data-state="complete"/g)).toHaveLength(1);
    expect(card(connection, [], { testing: true })).not.toContain('badge green');
  });
  it('preserves server-authoritative edit restrictions even for an administrator', () => {
    const html = card({ ...connection, can_edit: false }, [], { admin: true });
    expect(html).not.toContain('aria-label="编辑 订单服务"');
    expect(html).not.toContain('aria-label="删除 订单服务"');
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*>.*?测试连接<\/button>/);
    expect(html).toContain('查看工具');
    expect(html).toContain('配置助手');
  });
});

describe('onboarding network boundaries', () => {
  it('loads only authorized connections and assistants and retains a partial read failure', async () => {
    const fetch = vi.fn().mockImplementation((url: string) => Promise.resolve(url.endsWith('/connections') ? new Response(JSON.stringify([connection])) : new Response(JSON.stringify({ error: { code: 'FORBIDDEN', message: '不能读取助手' } }), { status: 403 })));
    vi.stubGlobal('fetch', fetch);
    const signal = new AbortController().signal;
    const result = await loadConnectionOnboarding(signal);
    expect(result.connections).toMatchObject({ status: 'fulfilled', value: [connection] });
    expect(result.assistants.status).toBe('rejected');
    expect(fetch.mock.calls.map(([url]) => url)).toEqual(['/api/v1/connections', '/api/v1/assistants']);
    expect(fetch.mock.calls.every(([, init]) => init.signal === signal)).toBe(true);
  });
  it('saving configuration does not discover tools, bind assistants, or run business tools', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ...connection, status: 'untested' })));
    vi.stubGlobal('fetch', fetch);
    const body = { name: '订单服务', scope: 'private', source_metadata: { timezone: 'Asia/Shanghai' }, transport: 'http', url: connection.url, auth_type: 'none', enabled: true };
    const saved = await saveConnectionConfiguration(null, body);
    expect(saved.status).toBe('untested');
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0][0]).toBe('/api/v1/connections');
    expect(fetch.mock.calls[0][1].method).toBe('POST');
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual(body);
  });
  it('editing preserves metadata and omitted credentials and only explicit test calls discovery', async () => {
    const fetch = vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify(connection))));
    vi.stubGlobal('fetch', fetch);
    const body = { name: '订单服务', source_metadata: { tables: 'orders' }, auth_type: 'bearer' };
    await saveConnectionConfiguration({ ...connection, id: 'orders/1' }, body);
    expect(fetch.mock.calls[0][0]).toBe('/api/v1/connections/orders%2F1');
    expect(fetch.mock.calls[0][1].method).toBe('PATCH');
    expect(JSON.parse(fetch.mock.calls[0][1].body)).not.toHaveProperty('credential');
    await testConnectionDiscovery(connection);
    expect(fetch.mock.calls[1][0]).toBe('/api/v1/connections/orders/test');
    expect(fetch.mock.calls[1][1].method).toBe('POST');
    expect(fetch).toHaveBeenCalledTimes(2);
  });
  it('propagates a save failure without retrying or starting a test', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: { code: 'CREDENTIAL_REQUIRED', message: 'Bearer 认证需要凭据' } }), { status: 422 }));
    vi.stubGlobal('fetch', fetch);
    await expect(saveConnectionConfiguration(connection, { auth_type: 'bearer', clear_credential: true })).rejects.toMatchObject({ code: 'CREDENTIAL_REQUIRED' });
    expect(fetch).toHaveBeenCalledTimes(1);
  });
});

describe('connection editor controls', () => {
  it('offers only supported authentication and explicit save versus save-and-test actions', () => {
    const html = renderToStaticMarkup(<ConnectionEditor value={null} admin={false} onClose={noop} onSaved={noop}/>);
    expect(html).toContain('Streamable HTTP');
    expect(html).toContain('由工作台服务端连接');
    expect(html).toContain('value="none"');
    expect(html).toContain('value="bearer"');
    expect(html).not.toContain('OAuth');
    expect(html).toContain('value="save"');
    expect(html).toContain('value="test"');
    expect(html).toContain('不会运行业务工具或自动绑定助手');
  });
  it('keeps credential replacement, clear and data-source controls without revealing a saved token', () => {
    const html = renderToStaticMarkup(<ConnectionEditor value={{ ...connection, auth_type: 'bearer', credential_present: true, source_metadata: { timezone: 'Asia/Shanghai' } }} admin={false} onClose={noop} onSaved={noop}/>);
    expect(html).toContain('替换 Token');
    expect(html).toContain('type="password"');
    expect(html).toContain('清除已保存的凭据');
    expect(html).toContain('Asia/Shanghai');
    expect(html).toContain('启用此连接');
  });
});
