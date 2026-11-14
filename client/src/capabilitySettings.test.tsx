import { afterEach, describe, expect, it, vi } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { CapabilitySettingsView } from './CapabilitySettingsPage';
import { capabilityInitialState, capabilityPageReducer, canManageCapabilities, loadCapabilitySettings, saveCapabilitySettings } from './capabilitySettings';
import type { CapabilityConfiguration, CapabilityPageState } from './capabilitySettings';

const fixture: CapabilityConfiguration = {
  revision: 0, origin: 'environment', updated_at: null,
  search: {enabled: true, preferred: 'bing_rss', fallbacks: [], timeout_seconds: 60},
  fetch: {enabled: true, preferred: 'public_http', fallbacks: [], timeout_seconds: 60},
  providers: [
    {id: 'bing_rss', configured: true, verification: 'not_tested', capabilities: ['search']},
    {id: 'public_http', configured: true, verification: 'not_tested', capabilities: ['fetch']},
    {id: 'deepseek_api', configured: false, verification: 'not_tested', capabilities: ['search','fetch']},
  ],
  ocr: {status: 'not_integrated', configurable: false, knowledge_fallback_configured: false},
  image_search: {status: 'not_integrated', configurable: false},
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

  it('shows configured but not verified providers and unsupported abilities honestly', () => {
    const html = render(capabilityPageReducer(capabilityInitialState, {type:'loaded', value:fixture}));
    expect(html).toContain('沿用部署环境默认值');
    expect(html).toContain('已配置，待验证');
    expect(html).toContain('DeepSeek 官方扩展服务 · 未配置');
    expect(html).toContain('独立工具未接入');
    expect(html).toContain('不提供无效开关');
    expect(html).not.toContain('type="password"');
    expect((html.match(/type="checkbox"/g)||[]).length).toBe(2);
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
  it('loads and saves only the versioned editable route fields', async () => {
    const fetch = vi.fn().mockResolvedValueOnce(response(fixture)).mockResolvedValueOnce(response({...fixture, revision:1}));
    vi.stubGlobal('fetch', fetch);
    expect(await loadCapabilitySettings()).toEqual(fixture);
    expect((await saveCapabilitySettings(fixture)).revision).toBe(1);
    const [target, options] = fetch.mock.calls[1];
    expect(target).toBe('/api/v1/capability-settings');
    expect(options.method).toBe('PUT');
    expect(options.credentials).toBe('include');
    expect(JSON.parse(options.body)).toEqual({revision:0, search:fixture.search, fetch:fixture.fetch});
    expect(options.body).not.toContain('ocr');
    expect(options.body).not.toContain('key');
  });

  it('propagates API conflict and loading failures for the visible error state', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(response({error:{code:'CAPABILITY_REVISION_CONFLICT',message:'重新加载配置'}},409))
      .mockRejectedValueOnce(new TypeError('network')));
    await expect(saveCapabilitySettings(fixture)).rejects.toMatchObject({status:409, code:'CAPABILITY_REVISION_CONFLICT'});
    await expect(loadCapabilitySettings()).rejects.toMatchObject({code:'NETWORK_ERROR'});
  });
});
