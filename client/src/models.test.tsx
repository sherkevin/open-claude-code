import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import ModelSelector from './ModelSelector';
import ModelRecovery from './ModelRecovery';
import { conversationHasModelHistory, modelSelection, modelSwitchRequiresNewConversation, publicModelLabel, saveModelSelection } from './models';
import type { ModelOption } from './types';

const models: ModelOption[] = [
  { id: 'dano-v2.1', label: 'Dano-v2.1', available: true, reasoning_levels: ['light', 'high', 'max'].map(id => ({ id, label: id })), default_reasoning_level: 'high' },
  { id: 'glm-5.3', label: 'GLM-5.3', available: true, reasoning_levels: [{ id: 'default', label: '默认' }], default_reasoning_level: 'default', requires_new_conversation_on_switch: true },
  { id: 'deepseek-v4.1-flash', label: 'deepseek-v4.1-flash', available: true, reasoning_levels: [{ id: 'default', label: '默认' }], default_reasoning_level: 'default' },
];

afterEach(() => { vi.unstubAllGlobals(); });

describe('public model selection', () => {
  it('preserves an explicit selection and uses only levels supported by its catalog entry', () => {
    const defaults = { default_model_id: 'dano-v2.1', default_reasoning_level: 'light' };
    expect(modelSelection(models, defaults)).toEqual({ model_id: 'dano-v2.1', reasoning_level: 'light' });
    expect(modelSelection(models, defaults, { model_id: 'glm-5.3', reasoning_level: 'default' })).toEqual({ model_id: 'glm-5.3', reasoning_level: 'default' });
    expect(modelSelection(models, defaults, { model_id: 'glm-5.3', reasoning_level: 'max' }).reasoning_level).toBe('default');
  });
  it('does not silently replace an unavailable configured model with another provider', () => {
    const unavailable = [{ ...models[0], available: false, unavailable_reason: '线路尚未启用' }, models[1]];
    expect(modelSelection(unavailable, { default_model_id: 'dano-v2.1' }).model_id).toBe('dano-v2.1');
    const html = renderToStaticMarkup(<ModelSelector models={unavailable} value={{ model_id: 'dano-v2.1', reasoning_level: 'high' }} onChange={() => {}}/>);
    expect(html).toMatch(/role="option" aria-selected="true" tabindex="0" disabled=""/);
    expect(html).toContain('线路尚未启用');
  });
  it('never renders unknown internal routing IDs as public labels', () => {
    expect(publicModelLabel(models, 'dano-v2.1')).toBe('Dano-v2.1');
    expect(publicModelLabel(models, 'glm-5.3')).toBe('GLM-5.3');
    expect(publicModelLabel(models, 'private-provider-model-1234')).toBe('模型信息暂不可用');
  });
  it('locks both model controls while a change or task is pending', () => {
    const html = renderToStaticMarkup(<ModelSelector models={models} value={{ model_id: 'dano-v2.1', reasoning_level: 'max' }} disabled disabledReason="当前任务完成或停止后可以切换模型。" onChange={() => {}}/>);
    expect(html.match(/<button[^>]*aria-label="选择模型"[^>]*>/)?.[0]).toContain('disabled=""');
    expect(html.match(/<input[^>]*type="range"[^>]*>/)?.[0]).toContain('disabled=""');
    expect(html).toContain('aria-valuetext="最高"');
    expect(html).toContain('当前任务完成或停止后可以切换模型。');
  });
  it('blocks both directions of incompatible native history, but allows same-model levels and compatible switches', () => {
    expect(modelSwitchRequiresNewConversation(models, 'dano-v2.1', 'glm-5.3', true)).toBe(true);
    expect(modelSwitchRequiresNewConversation(models, 'glm-5.3', 'deepseek-v4.1-flash', true)).toBe(true);
    expect(modelSwitchRequiresNewConversation(models, 'glm-5.3', 'glm-5.3', true)).toBe(false);
    expect(modelSwitchRequiresNewConversation(models, 'dano-v2.1', 'dano-v2.1', true)).toBe(false);
    expect(modelSwitchRequiresNewConversation(models, 'dano-v2.1', 'deepseek-v4.1-flash', true)).toBe(false);
    expect(modelSwitchRequiresNewConversation(models, 'dano-v2.1', 'glm-5.3', false)).toBe(false);
  });
  it('uses native-session status before legacy message-count fallback, including failure before session creation', () => {
    expect(conversationHasModelHistory(true, 0)).toBe(true);
    expect(conversationHasModelHistory(false, 2)).toBe(false);
    expect(conversationHasModelHistory(undefined, 2)).toBe(true);
    expect(conversationHasModelHistory(undefined, 0)).toBe(false);
  });
  it('marks incompatible chat options and preserves an enabled reasoning selector on Dano', () => {
    const html = renderToStaticMarkup(<ModelSelector models={models} value={{ model_id: 'dano-v2.1', reasoning_level: 'high' }} hasHistory onChange={() => {}}/>);
    expect(html).toMatch(/role="option"[^>]*disabled=""[^>]*><span><strong>GLM-5.3<\/strong><small>需新对话/);
    expect(html).toMatch(/role="option" aria-selected="false" tabindex="-1" class="fw-model-option"><span><strong>deepseek-v4.1-flash/);
    expect(html).toContain('当前对话和记忆保留');
    expect(html.match(/<input[^>]*type="range"[^>]*>/)?.[0]).not.toContain('disabled');
    const glm = renderToStaticMarkup(<ModelSelector models={models} value={{ model_id: 'glm-5.3', reasoning_level: 'default' }} hasHistory onChange={() => {}}/>);
    expect(glm).toMatch(/role="option"[^>]*disabled=""[^>]*><span><strong>Dano-v2.1/);
    expect(glm).toMatch(/role="option"[^>]*disabled=""[^>]*><span><strong>deepseek-v4.1-flash/);
    expect(glm).toMatch(/role="option" aria-selected="true" tabindex="0" class="fw-model-option"><span><strong>GLM-5.3/);
    expect(glm).not.toContain('type="range"');
    expect(glm).toContain('固定推理级别');
  });
  it('keeps all enabled models selectable in a new or empty conversation', () => {
    const html = renderToStaticMarkup(<ModelSelector models={models} value={{ model_id: 'dano-v2.1', reasoning_level: 'high' }} onChange={() => {}}/>);
    expect(html).toMatch(/role="option" aria-selected="false" tabindex="-1" class="fw-model-option"><span><strong>GLM-5.3/);
    expect(html).not.toContain('需新对话');
  });
  it('lists available models first and removes repeated capability labels', () => {
    const catalog = [
      { ...models[1], capabilities: ['general', 'chinese'] },
      { ...models[2], available: false, capabilities: ['multimodal'], unavailable_reason: '尚未开通' },
      { ...models[0], capabilities: ['industrial', 'multimodal'] },
      { ...models[1], id: 'hy4', label: '混元 4', capabilities: ['multimodal'] },
    ];
    const html = renderToStaticMarkup(<ModelSelector models={catalog} value={{ model_id: 'glm-5.3', reasoning_level: 'default' }} onChange={() => {}}/>);
    expect(html).toContain('<strong>HY4</strong>');
    expect(html.indexOf('<strong>Dano-v2.1</strong>')).toBeLessThan(html.indexOf('<strong>GLM-5.3</strong>'));
    expect(html).not.toContain('仅文字');
    expect(html).not.toContain('支持图片');
    expect(html).toContain('<strong>deepseek-v4.1-flash</strong><small>尚未开通</small>');
    const unknown = renderToStaticMarkup(<ModelSelector models={models} value={{ model_id: 'glm-5.3', reasoning_level: 'default' }} onChange={() => {}}/>);
    expect(unknown).not.toContain('仅文字');
    expect(unknown).not.toContain('支持图片');
  });
  it('offers an actionable new-conversation choice without disabling an available protected model', () => {
    const html = renderToStaticMarkup(<ModelSelector models={models} value={{ model_id: 'dano-v2.1', reasoning_level: 'high' }} hasHistory onChange={() => {}} onNewConversation={() => {}}/>);
    const option = html.match(/<button[^>]*role="option"[^>]*><span><strong>GLM-5.3[\s\S]*?<\/button>/)?.[0];
    expect(option).toBeDefined();
    expect(option).not.toContain('disabled=""');
    expect(option).toContain('新建会话使用');
    expect(html).toContain('当前对话和记忆保留');
  });
  it('renders every reasoning level provided by the model catalog', () => {
    const reasoning_levels = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh'].map(id => ({ id, label: id }));
    const catalog = [{ ...models[0], reasoning_levels }];
    const html = renderToStaticMarkup(<ModelSelector models={catalog} value={{ model_id: catalog[0].id, reasoning_level: 'high' }} onChange={() => {}}/>);
    expect(html).toMatch(/type="range"[^>]*max="5"/);
    expect(html).toContain('>很高</span>');
  });
  it('fills the track to the selected stop and reserves peak motion for the last available level', () => {
    function renderLevels(ids: string[], selected: string) {
      const model = { ...models[0], reasoning_levels: ids.map(id => ({ id, label: id })) };
      return renderToStaticMarkup(<ModelSelector models={[model]} value={{ model_id: model.id, reasoning_level: selected }} onChange={() => {}}/>);
    }
    const dano = ['light', 'high', 'max'];
    const high = renderLevels(dano, 'high');
    expect(high).toContain('--fw-stop:50%');
    expect(high.match(/<i /g)).toHaveLength(3);
    expect(high.match(/<i data-filled=/g)).toHaveLength(2);
    expect(high).not.toContain('data-peak=');
    expect(renderLevels(dano, 'max')).toContain('data-peak="true"');

    const six = ['light', 'medium', 'high', 'xhigh', 'max', 'ultra'];
    expect(renderLevels(six, 'max')).not.toContain('data-peak=');
    expect(renderLevels(six, 'ultra')).toContain('--fw-stop:100%');
    expect(renderLevels(six, 'ultra')).toContain('data-peak="true"');
    expect(renderLevels(['default'], 'default')).not.toContain('data-peak=');
  });
  it('explicitly reconnects an unchanged public selection with PATCH and never submits a message', async () => {
    const selection = { model_id: 'glm-5.3', reasoning_level: 'default' };
    const canonical = { id: 'chat-id', ...selection, has_native_session: true };
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(canonical), { status: 200 }));
    vi.stubGlobal('fetch', fetch);
    expect(await saveModelSelection('chat-id', selection, selection)).toBeNull();
    expect(fetch).not.toHaveBeenCalled();
    expect(await saveModelSelection('chat-id', selection, selection, true)).toEqual(canonical);
    expect(fetch).toHaveBeenCalledTimes(1);
    const [url, request] = fetch.mock.calls[0];
    expect(url).toBe('/api/v1/conversations/chat-id');
    expect(request.method).toBe('PATCH');
    expect(JSON.parse(request.body)).toEqual({...selection,auto_model_routing:false});
    expect(fetch.mock.calls.some(([target, init]) => String(target).includes('/messages') || init.method === 'POST')).toBe(false);
  });
  it('never automatically retries failed reconnection and gives unknown model IDs no reconnect action', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: { code: 'MODEL_UNAVAILABLE', message: '模型暂不可用' } }), { status: 503 }));
    vi.stubGlobal('fetch', fetch);
    const selection = { model_id: 'dano-v2.1', reasoning_level: 'high' };
    await expect(saveModelSelection('chat-id', selection, selection, true)).rejects.toMatchObject({ code: 'MODEL_UNAVAILABLE' });
    expect(fetch).toHaveBeenCalledTimes(1);
    const unavailable = renderToStaticMarkup(<ModelRecovery currentAvailable={false} busy={false} pending={false} onReconnect={() => {}}/>);
    expect(unavailable).not.toContain('<button');
    expect(unavailable).toContain('请选择可用模型');
    const busy = renderToStaticMarkup(<ModelRecovery currentAvailable busy pending={false} onReconnect={() => {}}/>);
    expect(busy.match(/<button\b[^>]*>/)?.[0]).toContain('disabled');
    expect(busy).toContain('已有任务不会自动重放');
  });
});
