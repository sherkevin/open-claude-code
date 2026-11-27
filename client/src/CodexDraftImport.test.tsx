import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { appendEngineeringText, CodexDraftImport } from './CodexDraftImport';
import WorkspacePage from './WorkspacePage';
import type { Assistant } from './types';

const assistant: Assistant = {
  id: 'purchasing', name: '采购助手', description: '查询已授权采购资料', instructions: '',
  enabled: true, access: { all_members: true, user_ids: [] }, revision: 1,
  connections: [{ connection_id: 'mcp', allowed_tools: ['read'], auto_approved_tools: [] }],
  knowledge_base_ids: ['kb'],
};

const incoming = '  第二条要求\n  保留两端空格  ';
const existing = '  工程稿的第一条\n';
const entry = { text: incoming, ordinaryPending: false };
const props = { entry, ready: true, occupied: true, canEdit: true, revision: 'original', text: existing, edit: vi.fn() };
describe('普通输入框打开工程任务', () => {
  it('preserves both editable documents verbatim when a user explicitly appends', () => {
    expect(appendEngineeringText(existing, incoming)).toBe(existing + '\n' + incoming);
    expect(appendEngineeringText('', incoming)).toBe(incoming);
    expect(appendEngineeringText(existing, '')).toBe(existing);
    const html = renderToStaticMarkup(<CodexDraftImport {...props}/>);
    expect(html).toContain('来自普通输入框的文字');
    expect(html).toContain(incoming);
    expect(html).toContain('追加到工程草稿');
    expect(props.edit).not.toHaveBeenCalled();
  });

  it('withholds append for unconfirmed submissions and oversized combined text', () => {
    const view=(extra:Partial<typeof props>)=>renderToStaticMarkup(<CodexDraftImport {...props} {...extra}/>);
    expect(view({ canEdit: false })).toContain('请先核对原工程请求');
    expect(view({entry:{...entry,ordinaryPending:true}})).toContain('普通聊天有待核对');
    expect(view({text:'a'.repeat(20000)})).toContain('超过 20,000 个字符');
    for (const extra of [{canEdit:false},{entry:{...entry,ordinaryPending:true}},{text:'a'.repeat(20000)}]) expect(view(extra)).not.toContain('追加到工程草稿');
  });

  it('keeps ordinary submit and exposes a separate non-submit action only when available', () => {

    const workspace=(codexEnabled:boolean,canCreate=true)=>renderToStaticMarkup(<WorkspacePage assistants={[assistant]} conversations={[]}
      onAssistant={()=>{}} onConversation={()=>{}} onNavigate={()=>{}} onStart={()=>{}} onEngineering={()=>{}}
      canCreate={canCreate} codexEnabled={codexEnabled}/>);
    expect(workspace(true)).toMatch(/<button[^>]*type="button"[^>]*>工程任务<\/button>/);
    expect(workspace(true)).toMatch(/<button[^>]*type="submit"[^>]*>发送/);
    expect(workspace(false)).not.toContain('>工程任务</button>');
    expect(workspace(true,false)).not.toContain('>工程任务</button>');
  });
});
