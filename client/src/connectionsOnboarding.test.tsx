import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from './api';
import { ConnectionEditor, ConnectionOnboardingCard, McpToolDetails, connectionDiscoveryState, connectionTestError, loadConnections, mcpToolPurpose, saveConnectionConfiguration, testConnectionDiscovery } from './ConnectionsPage';
import type { Connection } from './types';

const connection: Connection = { id: 'orders', name: '订单服务', transport: 'http', url: 'https://mcp.example/mcp', auth_type: 'none', credential_present: false, enabled: true, status: 'connected', tools: [{ name: 'orders_total', description: '汇总订单', inputSchema: {} }], last_error: null, last_tested_at: '2026-11-14T09:00:00Z', revision: 2, scope: 'private', can_edit: true };
const noop = () => {};
const card = (item: Connection = connection, extras: Partial<Parameters<typeof ConnectionOnboardingCard>[0]> = {}) => renderToStaticMarkup(<ConnectionOnboardingCard item={item} admin={false} onTest={noop} onTools={noop} onEdit={noop} onDelete={noop} {...extras}/>);
afterEach(() => vi.unstubAllGlobals());

describe('MCP resource catalog', () => {
  it('shows name, functional entries, tool list and status without assistant setup or raw URL', () => {
    const html = card();
    for (const text of ['订单服务', '汇总订单', '工具列表 · 1', '已发现 1 个工具', '测试连接']) expect(html).toContain(text);
    for (const text of ['配置助手', '绑定助手', '开始查询', '服务端如何连接', connection.url]) expect(html).not.toContain(text);
  });
  it('keeps read-only configuration in advanced settings, not a mandatory onboarding step', () => {
    const html = card({...connection, read_only_review_revision: 0}, {onReadOnlyReview: noop, onBusinessTrial: noop});
    expect(html).toContain('高级审批设置');
    expect(html).toContain('平台已识别的只读工具');
    expect(html).toContain('验证业务结果');
  });
  it('offers a real business trial separately from discovery, only after connection testing', () => {
    const html = card(connection, { onBusinessTrial: noop });
    expect(html).toContain('验证业务结果');
    expect(html).toContain('业务执行需另行验证');
    for (const item of [{ ...connection, status: 'failed' as const }, { ...connection, tools: [] }, { ...connection, enabled: false }]) {
      expect(card(item, { onBusinessTrial: noop })).not.toContain('验证业务结果');
    }
    expect(card(connection, { onBusinessTrial: noop, testError: 'request failed' })).not.toContain('验证业务结果');
  });
  it('does not treat empty discovery, stale timestamps or disabled connections as ready', () => {
    expect(connectionDiscoveryState({ ...connection, tools: [] })).toMatchObject({ label: '未发现工具', complete: false });
    expect(connectionDiscoveryState({ ...connection, status: 'untested' }).complete).toBe(false);
    expect(card({ ...connection, status: 'untested' })).toContain('配置已更改，待重测');
    expect(connectionDiscoveryState({ ...connection, enabled: false }).complete).toBe(false);
  });
  it('distinguishes remote auth and workbench permission failures', () => {
    expect(connectionDiscoveryState({ ...connection, status: 'needs_auth' }).label).toBe('认证未通过');
    expect(connectionDiscoveryState({ ...connection, status: 'failed' }).label).toBe('测试失败');
    expect(connectionTestError(new ApiError('Forbidden', 403, 'FORBIDDEN'))).toContain('当前账号无权测试');
    expect(connectionTestError(new ApiError('Expired', 401, 'UNAUTHORIZED'))).toContain('工作台登录已失效');
  });
  it('does not retain success styling after a request fails or while testing', () => {
    const failed = card(connection, { testError: '测试请求未完成' });
    expect(failed).toContain('本次测试未完成'); expect(failed).not.toContain('badge green');
    expect(card(connection, { testing: true })).not.toContain('badge green');
  });
  it('preserves server-authoritative edit restrictions even for an administrator', () => {
    const html = card({ ...connection, can_edit: false }, { admin: true });
    expect(html).not.toContain('aria-label="编辑 订单服务"'); expect(html).not.toContain('aria-label="删除 订单服务"');
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*>.*?测试连接<\/button>/);
    expect(html).toContain('工具列表'); expect(html).not.toContain('配置助手');
  });
  it('shortens only known functional copy and retains source metadata and tool definitions', () => {
    const tool = { name: 'rules', description: 'Search synthetic Chinese company rules; always returns source IDs and labels data as demo.', inputSchema: { type: 'object', properties: { query: { type: 'string', description: '关键词' } }, required: ['query'] } };
    const original = JSON.stringify(tool), item = { ...connection, tools: [tool], source_metadata: { data_type: 'synthetic' } };
    expect(mcpToolPurpose(tool)).toBe('检索中文企业制度资料，返回来源编号。');
    const summary = card(item); expect(summary).not.toContain('示例'); expect(summary).not.toContain('真实客户');
    const detail = renderToStaticMarkup(<McpToolDetails item={item} tool={tool}/>);
    expect(detail).toContain('关键词'); expect(detail).toContain('必填'); expect(detail).toContain('synthetic');
    expect(detail).toContain('原始定义与数据来源'); expect(JSON.stringify(tool)).toBe(original);
    const custom = { ...tool, description: 'Only available in the staging tenant.' };
    expect(mcpToolPurpose(custom)).toBe(custom.description);
  });
  it('keeps diagnostic tools out of business summaries without removing their definitions', () => {
    const diagnostic = { name: 'oversized_result', description: '超限验收专用：故意返回201行演示数据；宿主应拒绝，不得把样本当全量。', inputSchema: {} };
    const item = { ...connection, tools: [...connection.tools, diagnostic] };
    const html = card(item);
    expect(html).toContain('汇总订单');
    expect(html).toContain('工具列表 · 2');
    expect(html).not.toContain('超限');
    const detail = renderToStaticMarkup(<McpToolDetails item={item} tool={diagnostic}/>);
    expect(detail).toContain('连接诊断：检查超出明细上限时的拦截，不修改记录。');
    expect(detail).toContain(diagnostic.name);
    expect(detail).toContain(diagnostic.description);
  });
});

describe('MCP resource network boundaries', () => {
  it('loads only authorized connections without reading or modifying assistants', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify([connection]))); vi.stubGlobal('fetch', fetch);
    const signal = new AbortController().signal;
    expect(await loadConnections(signal)).toEqual([connection]);
    expect(fetch.mock.calls.map(([url]) => url)).toEqual(['/api/v1/connections']);
    expect(fetch.mock.calls[0][1].signal).toBe(signal);
  });
  it('saving configuration never invokes tool discovery or business tools', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ...connection, status: 'untested' }))); vi.stubGlobal('fetch', fetch);
    const body = { name: '订单服务', scope: 'private', source_metadata: { timezone: 'Asia/Shanghai' }, transport: 'http', url: connection.url, auth_type: 'none', enabled: true };
    expect((await saveConnectionConfiguration(null, body)).status).toBe('untested');
    expect(fetch).toHaveBeenCalledTimes(1); expect(fetch.mock.calls[0][0]).toBe('/api/v1/connections');
    expect(fetch.mock.calls[0][1].method).toBe('POST'); expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual(body);
  });
  it('editing preserves metadata and omitted credentials and only explicit test calls discovery', async () => {
    const fetch = vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify(connection)))); vi.stubGlobal('fetch', fetch);
    const body = { name: '订单服务', source_metadata: { tables: 'orders' }, auth_type: 'bearer' };
    await saveConnectionConfiguration({ ...connection, id: 'orders/1' }, body);
    expect(fetch.mock.calls[0][0]).toBe('/api/v1/connections/orders%2F1'); expect(fetch.mock.calls[0][1].method).toBe('PATCH');
    expect(JSON.parse(fetch.mock.calls[0][1].body)).not.toHaveProperty('credential');
    await testConnectionDiscovery(connection);
    expect(fetch.mock.calls[1][0]).toBe('/api/v1/connections/orders/test'); expect(fetch.mock.calls[1][1].method).toBe('POST'); expect(fetch).toHaveBeenCalledTimes(2);
  });
  it('propagates a save failure without retrying or starting a test', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: { code: 'CREDENTIAL_REQUIRED', message: 'Bearer 认证需要凭据' } }), { status: 422 })); vi.stubGlobal('fetch', fetch);
    await expect(saveConnectionConfiguration(connection, { auth_type: 'bearer', clear_credential: true })).rejects.toMatchObject({ code: 'CREDENTIAL_REQUIRED' }); expect(fetch).toHaveBeenCalledTimes(1);
  });
});

describe('MCP editor controls', () => {
  it('offers only supported authentication and explicit save versus save-and-test actions', () => {
    const html = renderToStaticMarkup(<ConnectionEditor value={null} admin={false} onClose={noop} onSaved={noop}/>);
    for (const value of ['Streamable HTTP', 'value="none"', 'value="bearer"', 'value="save"', 'value="test"', '不运行业务工具']) expect(html).toContain(value);
    expect(html).not.toContain('OAuth'); expect(html).not.toContain('绑定助手');
  });
  it('keeps credential replacement, clear and data-source controls without revealing a saved token', () => {
    const html = renderToStaticMarkup(<ConnectionEditor value={{ ...connection, auth_type: 'bearer', credential_present: true, source_metadata: { timezone: 'Asia/Shanghai' } }} admin={false} onClose={noop} onSaved={noop}/>);
    for (const value of ['替换 Token', 'type="password"', '清除已保存的凭据', 'Asia/Shanghai', '启用此连接']) expect(html).toContain(value);
  });
});
