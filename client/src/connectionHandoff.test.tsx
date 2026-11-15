import { afterEach, describe, expect, it, vi } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import AssistantConfiguration, { assistantChanges, assistantDraft, type AssistantResources } from './AssistantConfiguration';
import AssistantsPage from './AssistantsPage';
import NewSession, { createNewSession } from './NewSession';
import { connectionHandoffReadiness, connectionSetupContext, focusConnectionFirst, loadConnectionHandoff } from './connectionHandoff';
import type { Assistant, Connection } from './types';

const connection: Connection = { id: 'target', name: '发货查询', transport: 'http', url: 'https://example.test/mcp', auth_type: 'bearer', credential_present: true, enabled: true, status: 'connected', tools: [{ name: 'shipments', description: '按日期查询', inputSchema: {} }], last_error: null, last_tested_at: '2026-11-15T01:00:00Z', revision: 2, scope: 'team', can_edit: false };
const assistant: Assistant = { id: 'assistant', name: '发货助手', description: '', instructions: '', enabled: true, scope: 'private', can_edit: true, access: { all_members: false, user_ids: [] }, connections: [{ connection_id: 'target', allowed_tools: ['shipments'], auto_approved_tools: [] }], revision: 3 };
const resources: AssistantResources = { connections: [{ ...connection, id: 'other', name: '其他连接' }, connection], knowledge: [], skills: [], users: [], models: [] };
const callbacks = { onSaved: () => {}, onDirtyChange: () => {}, onSavingChange: () => {}, onReload: () => {} };
const dataSection = (html: string) => html.split('aria-label="数据连接与工具"')[1].split('aria-label="知识库"')[0];
afterEach(() => vi.unstubAllGlobals());

describe('connection handoff preserves identity and explicit authorization', () => {
  it('carries only the connection identity, not endpoint, credentials or a stale tool snapshot', () => {
    expect(connectionSetupContext(connection)).toEqual({ id: 'target', name: '发货查询' });
  });
  it('puts the focused connection first without mutating the authorized list or inventing missing resources', () => {
    const original = [...resources.connections];
    expect(focusConnectionFirst(original, 'target').map(item => item.id)).toEqual(['target', 'other']);
    expect(original.map(item => item.id)).toEqual(['other', 'target']);
    expect(focusConnectionFirst(original, 'missing')).toEqual(original);
    expect(focusConnectionFirst(original)[0]).toBe(original[0]);
  });
  it.each([
    ['unavailable connection', undefined, assistant],
    ['another connection', { ...connection, id: 'other' }, assistant],
    ['disabled connection', { ...connection, enabled: false }, assistant],
    ['untested connection', { ...connection, status: 'untested' as const }, assistant],
    ['failed connection', { ...connection, status: 'failed' as const }, assistant],
    ['expired MCP credentials', { ...connection, status: 'needs_auth' as const }, assistant],
    ['empty discovery', { ...connection, tools: [] }, assistant],
    ['assistant not usable by this member', connection, undefined],
    ['disabled assistant', connection, { ...assistant, enabled: false }],
    ['unbound assistant', connection, { ...assistant, connections: [] }],
    ['binding to another connection', connection, { ...assistant, connections: [{ ...assistant.connections[0], connection_id: 'other' }] }],
    ['no allowed tools', connection, { ...assistant, connections: [{ ...assistant.connections[0], allowed_tools: [] }] }],
    ['only previously discovered tools', connection, { ...assistant, connections: [{ ...assistant.connections[0], allowed_tools: ['retired'] }] }],
    ['auto approval without an allowed tool', connection, { ...assistant, connections: [{ ...assistant.connections[0], allowed_tools: [], auto_approved_tools: ['shipments'] }] }],
  ])('does not offer a business trial for %s', (_label, currentConnection, currentAssistant) => {
    expect(connectionHandoffReadiness('target', currentConnection, currentAssistant).ready).toBe(false);
  });
  it('allows a trial with a shared connection without needing connection edit rights or auto approval, but never claims verified business results', () => {
    const before = structuredClone(assistant);
    const state = connectionHandoffReadiness('target', connection, assistant);
    expect(state.ready).toBe(true);
    expect(state.message).toContain('业务结果尚未验证');
    expect(assistant).toEqual(before);
  });
  it('highlights a new assistant target without binding it or selecting any tools', () => {
    const html = renderToStaticMarkup(<AssistantConfiguration value={null} resources={resources} admin={false} focusConnectionId="target" {...callbacks}/>);
    const section = dataSection(html);
    expect(section).toContain('connection-setup-focus');
    expect(section).toContain('不会自动授权');
    expect(section.indexOf('发货查询')).toBeLessThan(section.indexOf('其他连接'));
    expect(section).not.toContain('checked=""');
    expect(section).not.toContain('binding-permissions');
    expect(assistantDraft(null, resources).connections).toEqual([]);
  });
  it('preserves existing grants and read-only editing restrictions while focusing the target', () => {
    const value = { ...assistant, can_edit: false, connections: [...assistant.connections, { connection_id: 'hidden', allowed_tools: ['read'], auto_approved_tools: [], argument_constraints: { read: { properties: { date: { const: '2026-11-14' } } } } }] };
    const baseline = assistantDraft(value, resources);
    const html = renderToStaticMarkup(<AssistantConfiguration value={value} resources={resources} admin={false} focusConnectionId="target" {...callbacks}/>);
    expect(html).toContain('<fieldset class="plain-fieldset" disabled="">');
    expect(html).toContain('你只有查看权限');
    expect(html).not.toContain('保存助手');
    expect(html).toContain('无法访问的数据连接');
    expect(assistantChanges(baseline, assistantDraft(value, resources))).toEqual({});
    expect(value.connections).toEqual(baseline.connections);
  });
  it('does not expose a private or disabled unbound target just because it was selected on another page', () => {
    for (const focused of [{ ...connection, scope: 'private' as const }, { ...connection, enabled: false }]) {
      const html = renderToStaticMarkup(<AssistantConfiguration value={{ ...assistant, scope: 'team', connections: [] }} resources={{ ...resources, connections: [focused] }} admin={false} focusConnectionId="target" {...callbacks}/>);
      expect(html).toContain('目标连接当前不在可绑定列表中');
      expect(dataSection(html)).not.toContain('connection-setup-focus');
      expect(dataSection(html)).not.toContain('发货查询');
    }
  });
  it('offers new or existing assistant setup without implying a trial succeeded or auto-opening an editor', () => {
    const html = renderToStaticMarkup(<AssistantsPage admin={false} connectionSetup={connectionSetupContext(connection)} onChanged={() => {}} onOpen={() => {}} onReturnToConnection={() => {}} onStartAssistant={() => {}} onDirtyChange={() => {}} onSavingChange={() => {}} requestNavigation={action => action()} draftEpoch={0}/>);
    expect(html).toContain('为「发货查询」配置助手');
    expect(html).toContain('有编辑权限的现有助手');
    expect(html).toContain('新建助手');
    expect(html).toContain('返回该连接');
    expect(html).not.toContain('role="dialog"');
    expect(html).not.toContain('开始业务试跑');
  });
});

describe('post-save handoff refreshes permission snapshots', () => {
  it('reads only the current member-visible resources with cancellation and never invokes a tool', async () => {
    const fetch = vi.fn().mockImplementation((url: string) => Promise.resolve(new Response(JSON.stringify(url.endsWith('/connections') ? [connection] : [assistant]))));
    vi.stubGlobal('fetch', fetch);
    const controller = new AbortController();
    const result = await loadConnectionHandoff('target', 'assistant', controller.signal);
    expect(result.ready).toBe(true);
    expect(fetch.mock.calls.map(([url]) => url).sort()).toEqual(['/api/v1/assistants', '/api/v1/connections']);
    expect(fetch.mock.calls.every(([, options]) => options.signal === controller.signal && options.credentials === 'include' && !options.method && !options.body)).toBe(true);
  });
  it('rejects a now-revoked assistant or changed tool grant when the trial is rechecked', async () => {
    let current: Assistant[] = [assistant];
    vi.stubGlobal('fetch', vi.fn().mockImplementation((url: string) => Promise.resolve(new Response(JSON.stringify(url.endsWith('/connections') ? [connection] : current)))));
    expect((await loadConnectionHandoff('target', 'assistant')).ready).toBe(true);
    current = [{ ...assistant, connections: [] }];
    expect((await loadConnectionHandoff('target', 'assistant')).ready).toBe(false);
    current = [];
    expect((await loadConnectionHandoff('target', 'assistant')).assistant).toBeUndefined();
  });
  it('keeps authorization/network failures distinct from an empty usable list', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify({ error: { code: 'FORBIDDEN', message: '无权读取' } }), { status: 403 }))));
    await expect(loadConnectionHandoff('target', 'assistant')).rejects.toMatchObject({ status: 403, code: 'FORBIDDEN' });
  });
});

describe('connection trial session remains scoped to its configured assistant', () => {
  const selection = { model_id: 'dano-v2.1', reasoning_level: 'high' };
  const setup = connectionSetupContext(connection);
  const other = { ...assistant, id: 'unrelated', name: '无关助手', connections: [] };
  const sessionProps = { assistants: [assistant, other], initialAssistant: assistant, config: { brand_name: 'Flintworks', auth_types: [], model_label: '', upload_max_bytes: 1 }, models: [], modelsLoading: false, modelsError: '', onReloadModels: () => {}, onClose: () => {}, onCreated: () => {} };
  it('shows the target connection and fixes the assistant only for the guided trial', () => {
    const guided = renderToStaticMarkup(<NewSession {...sessionProps} connectionSetup={setup}/>);
    expect(guided).toContain('本次核对连接：发货查询');
    expect(guided).toMatch(/<select[^>]*disabled=""/);
    expect(guided).toContain('发货助手');
    expect(guided).not.toContain('无关助手');
    const ordinary = renderToStaticMarkup(<NewSession {...sessionProps}/>);
    expect(ordinary).not.toContain('本次核对连接');
    expect(ordinary.match(/<select[^>]*>/)?.[0]).not.toContain('disabled');
    expect(ordinary).toContain('无关助手');
  });
  it('rechecks before creating and sends only the original conversation payload', async () => {
    const fetch = vi.fn().mockImplementation((url: string) => Promise.resolve(new Response(JSON.stringify(url.endsWith('/connections') ? [connection] : url.endsWith('/assistants') ? [assistant] : { id: 'created' }))));
    vi.stubGlobal('fetch', fetch);
    await createNewSession(false, assistant.id, selection, false, setup);
    expect(fetch.mock.calls.map(([url]) => url)).toEqual(['/api/v1/connections', '/api/v1/assistants', '/api/v1/conversations']);
    expect(JSON.parse(fetch.mock.calls[2][1].body)).toEqual({ assistant_id: assistant.id, ...selection, auto_model_routing: false });
    expect(fetch.mock.calls.filter(([, init]) => init.method === 'POST')).toHaveLength(1);
  });
  it.each([
    ['revoked binding', connection, [{ ...assistant, connections: [] }], '尚未绑定此连接'],
    ['disabled connection', { ...connection, enabled: false }, [assistant], '此连接已停用'],
    ['revoked assistant access', connection, [], '当前账号无法使用此助手'],
  ])('blocks creation when submit-time checks find %s', async (_label, currentConnection, currentAssistants, message) => {
    const fetch = vi.fn().mockImplementation((url: string) => Promise.resolve(new Response(JSON.stringify(url.endsWith('/connections') ? [currentConnection] : currentAssistants))));
    vi.stubGlobal('fetch', fetch);
    await expect(createNewSession(false, assistant.id, selection, false, setup)).rejects.toThrow(message);
    expect(fetch.mock.calls).toHaveLength(2);
    expect(fetch.mock.calls.every(([, init]) => !init.method)).toBe(true);
  });
  it('leaves ordinary new-session creation free of connection checks', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response('{"id":"ordinary"}'));
    vi.stubGlobal('fetch', fetch);
    await createNewSession(false, other.id, selection, true);
    expect(fetch.mock.calls).toHaveLength(1);
    expect(fetch.mock.calls[0][0]).toBe('/api/v1/conversations');
  });
});
