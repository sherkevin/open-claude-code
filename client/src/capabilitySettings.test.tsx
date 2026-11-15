import { afterEach, describe, expect, it, vi } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { CapabilitySettingsView } from './CapabilitySettingsPage';
import {
  capabilityInitialState, capabilityPageReducer, canManageCapabilities, deleteProviderCredential, loadCapabilitySettings,
  runProviderDiagnostics, saveCapabilitySettings, saveProviderCredential,
} from './capabilitySettings';
import type { CapabilityConfiguration, CapabilityPageState, CapabilityRoute, ProviderView } from './capabilitySettings';

const route = (overrides: Partial<CapabilityRoute> = {}): CapabilityRoute => ({
  enabled: true, preferred: 'bing_rss', fallbacks: [], timeout_seconds: 60, max_attempts: 1,
  domains: {mode: 'public', hosts: []}, egress: {query: 'allow', url: 'allow', upload: 'deny'},
  limits: {calls_per_run: 50, calls_per_day: 500, max_upload_bytes: 0}, ...overrides,
});
const provider = (id: ProviderView['id'], overrides: Partial<ProviderView> = {}): ProviderView => ({
  id, configured: true, credential_present: false, verification: 'not_tested', verification_checks: {},
  capabilities: [], modes: {}, revision: 0, ...overrides,
});
const fixture: CapabilityConfiguration = {
  revision: 0, origin: 'environment', updated_at: null, schema_version: 2,
  search: route({preferred: 'deepseek_api'}), fetch: route({preferred: 'public_http'}), ocr: route({enabled: false, preferred: 'deepseek_api', limits: {calls_per_run: 20, calls_per_day: 100, max_upload_bytes: 20971520}}),
  image_search: route({enabled: false, preferred: 'deepseek_webtools'}),
  providers: [
    provider('bing_rss', {capabilities: ['search'], modes: {search: ['web']}}),
    provider('public_http', {capabilities: ['fetch'], modes: {fetch: ['page']}}),
    provider('deepseek_api', {configured: false, capabilities: ['search', 'fetch', 'ocr']}),
    provider('deepseek_webtools', {configured: false, capabilities: ['image_search']}),
  ],
  credentials: {
    bing_rss: {present: false, revision: 0, verified: false}, searxng: {present: false, revision: 0, verified: false},
    brave: {present: false, revision: 0, verified: false}, deepseek_api: {present: false, revision: 0, verified: false},
    public_http: {present: false, revision: 0, verified: false}, deepseek_webtools: {present: false, revision: 0, verified: false},
    brave_images: {present: false, revision: 0, verified: false},
  },
};
const render = (state: CapabilityPageState, allowed = true) => renderToStaticMarkup(<CapabilitySettingsView state={state} allowed={allowed} onReload={()=>{}} onSave={()=>{}} onChange={()=>{}}/>);
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), {status, headers: {'Content-Type': 'application/json'}});
afterEach(() => vi.unstubAllGlobals());

describe('capability settings permissions and presentation', () => {
  it('only exposes normal deployment administrators', () => {
    expect(canManageCapabilities(true, {})).toBe(true);
    expect(canManageCapabilities(false, {})).toBe(false);
    expect(canManageCapabilities(true, {deployment_mode:'trial'})).toBe(false);
    const html = render({...capabilityInitialState, value: fixture, phase: 'ready'}, false);
    expect(html).toContain('仅普通部署的工作台管理员');
    expect(html).not.toContain('保存并应用');
  });

  it('renders loading, error, empty, and retry without fake configuration', () => {
    expect(render(capabilityInitialState)).toContain('正在读取能力配置');
    const failed = capabilityPageReducer(capabilityInitialState, {type:'error', error:'网络失败'});
    const html = render(failed);
    expect(html).toContain('网络失败');
    expect(html).toContain('重新加载');
    expect(html).toContain('暂未取得配置');
    expect(html).not.toContain('保存并应用');
  });

  it('shows all four capabilities, safe defaults and no secret input without the credential panel', () => {
    const html = render(capabilityPageReducer(capabilityInitialState, {type:'loaded', value:fixture}));
    expect(html).toContain('沿用部署环境默认值');
    expect(html).toContain('网页搜索');
    expect(html).toContain('网页读取');
    expect(html).toContain('文档识别');
    expect(html).toContain('图片搜索');
    expect(html).toContain('文件外发');
    expect(html).toContain('已启用 · 待验证');
    expect(html).toContain('首选服务未配置');
    expect(html).not.toContain('type="password"');
    expect((html.match(/type="checkbox"/g)||[]).length).toBe(4);
  });

  it('preserves edits after failed save and only reports success after acknowledgment', () => {
    const loaded = capabilityPageReducer(capabilityInitialState, {type:'loaded', value:fixture});
    const saving = capabilityPageReducer(loaded, {type:'save'});
    expect(saving.phase).toBe('saving');
    expect(render(saving)).toContain('fieldset disabled');
    expect(render(saving)).not.toContain('配置已保存');
    const failed = capabilityPageReducer(saving, {type:'error', error:'配置已被其他管理员修改'});
    expect(failed.value).toEqual(fixture);
    expect(render(failed)).toContain('配置已被其他管理员修改');
    const saved = capabilityPageReducer(saving, {type:'saved', value:{...fixture, revision:1, origin:'administrator'}});
    expect(render(saved)).toContain('配置已保存并立即用于后续工具调用');
    expect(render(saved)).toContain('不代表外部线路已验证');
    expect(capabilityPageReducer(saved, {type:'edit', value:fixture}).saved).toBe(false);
  });
});

describe('capability settings actual API requests', () => {
  it('loads and saves all four versioned routes without any credential value', async () => {
    const fetch = vi.fn().mockResolvedValueOnce(response(fixture)).mockResolvedValueOnce(response({...fixture, revision:1}));
    vi.stubGlobal('fetch', fetch);
    expect(await loadCapabilitySettings()).toEqual(fixture);
    expect((await saveCapabilitySettings(fixture)).revision).toBe(1);
    const [target, options] = fetch.mock.calls[1];
    expect(target).toBe('/api/v1/capability-settings');
    expect(options.method).toBe('PUT');
    expect(options.credentials).toBe('include');
    expect(JSON.parse(options.body)).toEqual({
      revision: 0, search: fixture.search, fetch: fixture.fetch, ocr: fixture.ocr, image_search: fixture.image_search,
    });
    expect(options.body).not.toContain('credential');
  });

  it('uses write-only credential and bounded diagnostic endpoints', async () => {
    const fetch = vi.fn().mockImplementation(() => Promise.resolve(response({provider_id:'deepseek_api', present:false, revision:1, verified:false})));
    vi.stubGlobal('fetch', fetch);
    await saveProviderCredential('deepseek_api', 0, 'synthetic-value');
    const [putTarget, putInit] = fetch.mock.calls[0];
    expect(putTarget).toBe('/api/v1/capability-settings/providers/deepseek_api/credential');
    expect(putInit.method).toBe('PUT');
    expect(JSON.parse(putInit.body)).toEqual({revision: 0, credential: 'synthetic-value'});
    await deleteProviderCredential('deepseek_api', 1);
    expect(fetch.mock.calls[1][0]).toBe('/api/v1/capability-settings/providers/deepseek_api/credential');
    expect(fetch.mock.calls[1][1].method).toBe('DELETE');
    await runProviderDiagnostics('deepseek_api');
    const [diagTarget, diagInit] = fetch.mock.calls[2];
    expect(diagTarget).toBe('/api/v1/capability-settings/providers/deepseek_api/diagnostics');
    expect(diagInit.method).toBe('POST');
    // The diagnostic request carries no attacker-chosen target or query.
    expect(diagInit.body).toBeUndefined();
  });

  it('propagates API conflict and loading failures for the visible error state', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(response({error:{code:'CAPABILITY_REVISION_CONFLICT',message:'重新加载配置'}},409))
      .mockRejectedValueOnce(new TypeError('network')));
    await expect(saveCapabilitySettings(fixture)).rejects.toMatchObject({status:409, code:'CAPABILITY_REVISION_CONFLICT'});
    await expect(loadCapabilitySettings()).rejects.toMatchObject({code:'NETWORK_ERROR'});
  });
});
