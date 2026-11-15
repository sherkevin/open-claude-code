import type { ComponentProps } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import WorkspacePage from './WorkspacePage';
import { ConversationHistory } from './App';
import type { Assistant, Conversation } from './types';

const assistant: Assistant = {
  id: 'purchasing', name: '采购助手', description: '查询已授权采购资料', instructions: '',
  enabled: true, access: { all_members: true, user_ids: [] }, revision: 1,
  connections: [{ connection_id: 'mcp', allowed_tools: ['read'], auto_approved_tools: [] }],
  knowledge_base_ids: ['kb'],
};
const conversation = (id: string, updates: Partial<Conversation> = {}): Conversation => ({
  id, assistant_id: assistant.id, title: id, archived: false, active_run_id: null,
  created_at: '2026-11-14T00:00:00Z', updated_at: '2026-11-14T00:00:00Z',
  model_id: 'model', model_label: 'Model', reasoning_level: 'default', ...updates,
});
const render = (props: Partial<ComponentProps<typeof WorkspacePage>> = {}) => renderToStaticMarkup(
  <WorkspacePage assistants={[assistant]} conversations={[]} onAssistant={() => {}} onConversation={() => {}}
    onNavigate={() => {}} onNew={() => {}} {...props}/>,
);

describe('workspace real state and access boundaries', () => {
  it('shows only real active runs and excludes archived work without claiming other runs completed', () => {
    const html = render({ conversations: [
      conversation('正在查采购', { active_run_id: 'run-1' }),
      conversation('待继续的问题'),
      conversation('归档运行', { active_run_id: 'run-archived', archived: true }),
    ] });
    expect(html).toContain('1 项执行中');
    expect(html).toContain('待继续的问题');
    expect(html).not.toContain('归档运行');
    expect(html).not.toContain('已完成');
    expect(html).not.toContain('结果追溯');
    expect(html).not.toContain('节省');
  });

  it('orders recent work by actual time across timezone offsets and reports missing timestamps', () => {
    const html = render({ conversations: [
      conversation('较早本地时间', { updated_at: '2026-11-14T09:00:00+08:00' }),
      conversation('较晚UTC时间', { updated_at: '2026-11-14T02:00:00Z' }),
      conversation('无时间记录', { updated_at: '' }),
    ] });
    expect(html.indexOf('较晚UTC时间')).toBeLessThan(html.indexOf('较早本地时间'));
    expect(html.indexOf('较早本地时间')).toBeLessThan(html.indexOf('无时间记录'));
    expect(html).toContain('更新时间未提供');
    expect(html).not.toContain('Invalid Date');
  });

  it('does not expose inaccessible management, creation, resources or history', () => {
    const html = render({ canCreate: false, canManage: false, canReadKnowledge: false,
      canManageConnections: false, canReadSessions: false, onStart: () => {},
      conversations: [conversation('不应出现在受限首页')],
    });
    for (const value of ['管理助手', '配置助手', '新建会话', '开始会话', 'wr-question-panel', 'wr-resources', 'wr-recent', '不应出现在受限首页']) {
      expect(html).not.toContain(value);
    }
    expect(html).toContain('采购助手');
    expect(html).toContain('查看能力');
  });

  it('does not offer a dead configuration action when no assistant is available to a member', () => {
    const html = render({ assistants: [], canManage: false, onStart: () => {} });
    expect(html).toContain('还没有可用助手');
    expect(html).toContain('管理员开放助手后');
    expect(html).not.toContain('配置助手');
    expect(html).not.toContain('wr-question-panel');
  });

  it('offers a question field only when its handoff exists and never offers disabled assistants', () => {
    expect(render()).not.toContain('wr-question-panel');
    const html = render({ onStart: () => {}, assistants: [assistant, { ...assistant, id: 'disabled', name: '停用助手', enabled: false }] });
    expect(html).toContain('id="wr-question"');
    expect(html).toContain('进入会话后确认并发送');
    expect(html).toContain('value="purchasing"');
    expect(html).not.toContain('停用助手');
  });

  it('labels configured bindings as configuration, without claiming connection success', () => {
    const html = render();
    expect(html).toContain('已配置 1 个连接');
    expect(html).not.toContain('已连接');
    expect(html).not.toContain('只读优先');
  });
});


describe('workspace initial reads and stale snapshots', () => {
  const pending = { loading: true, loaded: false, error: '' };
  const failed = { loading: false, loaded: false, error: 'network unavailable' };
  const ready = { loading: false, loaded: true, error: '' };
  const renderHistory = (state: typeof pending, conversations: Conversation[] = []) => renderToStaticMarkup(
    <ConversationHistory state={state} conversations={conversations} onOpen={() => {}} onRetry={() => {}}/>,
  );

  it('shows pending reads instead of zero counts or empty data on the first authenticated frame', () => {
    const html = render({ assistants: [], conversations: [], assistantsRead: pending, conversationsRead: pending });
    expect(html).toContain('正在读取业务助手');
    expect(html).toContain('正在读取最近会话');
    expect(html).toContain('class="wr-count">—</span>');
    expect(html).not.toContain('还没有可用助手');
    expect(html).not.toContain('还没有会话');
    expect(html).not.toContain('>配置助手<');
    expect(html).toContain('管理助手');
    expect(html).toContain('全部会话');
    const history = renderHistory(pending);
    expect(history).toContain('正在读取最近会话');
    expect(history).not.toContain('从一项具体工作开始');
  });

  it('keeps independent successful sections usable while another resource is still pending', () => {
    const html = render({ assistantsRead: ready, conversationsRead: pending, onStart: () => {} });
    expect(html).toContain('采购助手');
    expect(html).toContain('wr-question-panel');
    expect(html).toContain('class="wr-count">1</span>');
    expect(html).toContain('正在读取最近会话');
    expect(html).not.toContain('还没有会话');
  });

  it('offers retry after first-read failures without implying the account has no data', () => {
    const html = render({ assistants: [], assistantsRead: failed, conversationsRead: failed, onReload: () => {} });
    expect(html).toContain('业务助手暂未读取成功');
    expect(html).toContain('最近会话暂未读取成功');
    expect(html.match(/>重新读取</g)).toHaveLength(2);
    expect(html).not.toContain('还没有可用助手');
    expect(html).not.toContain('还没有会话');
    expect(html).toContain('class="wr-count">—</span>');
    const history = renderHistory(failed);
    expect(history).toContain('最近会话暂未读取成功');
    expect(history).toContain('重新读取');
    expect(history).not.toContain('从一项具体工作开始');
  });

  it('preserves existing content during refresh and failed refresh, and exposes empty states only after success', () => {
    const conversations = [conversation('已保存的采购核对')];
    for (const state of [{ ...ready, loading: true }, { ...ready, error: 'network unavailable' }]) {
      const html = render({ assistantsRead: state, conversationsRead: state, conversations });
      expect(html).toContain('采购助手');
      expect(html).toContain('已保存的采购核对');
      expect(html).toContain('class="wr-count">1</span>');
      expect(html).not.toContain('还没有可用助手');
      expect(html).not.toContain('还没有会话');
      const history = renderHistory(state, conversations);
      expect(history).toContain('已保存的采购核对');
      if (state.error) {
        expect(html).toContain('当前仍显示上次读取的内容');
        expect(history).toContain('仍显示上次读取的内容');
      }
    }
    const empty = render({ assistants: [], assistantsRead: ready, conversationsRead: ready });
    expect(empty).toContain('还没有可用助手');
    expect(empty).toContain('还没有会话');
    expect(empty).toContain('class="wr-count">0</span>');
    expect(renderHistory(ready)).toContain('从一项具体工作开始');
  });
});
