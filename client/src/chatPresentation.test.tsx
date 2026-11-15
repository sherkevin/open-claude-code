import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import ChatPage, { PendingApprovals } from './ChatPage';
import type { AppConfig, Approval, Conversation } from './types';

describe('chat navigation and pending decisions', () => {
  it('keeps the trajectory entry and removes the separate execution sidebar', () => {
    const conversation = {id:'c',assistant_id:'a',title:'当前会话',model_id:'m',reasoning_level:'low',runtime_type:'harness',archived:false} as Conversation;
    const html = renderToStaticMarkup(<ChatPage conversation={conversation} config={{deployment_mode:'normal',brand_name:'开物',upload_max_bytes:1024,auth_types:[],model_label:'模型'} satisfies AppConfig} models={[]} modelsLoading={false} onChanged={()=>{}} onArchived={()=>{}}/>);
    expect(html).toContain('aria-label="内容轨迹"');
    expect(html).not.toContain('执行记录');
    expect(html).not.toContain('execution-panel');
    expect(html).not.toMatch(/DeepSeek Harness|Harness 上下文|Claude Agent SDK/i);
  });

  it('keeps approval controls available without any execution sidebar', () => {
    const approval = {id:'approval-1',run_id:'r',status:'pending',created_at:'2026-01-01T00:00:00Z',tool_name:'write_order',arguments:{order:'synthetic'},expires_at:'2099-01-01T00:00:00Z',risk_level:'high'} as Approval;
    const html = renderToStaticMarkup(<PendingApprovals conversationId="c" approvals={[approval]} busyId="" onDecision={async()=>true}/>);
    expect(html).toContain('aria-label="待确认操作"');
    expect(html).toContain('允许执行');
    expect(html).toContain('拒绝');
    expect(html).toContain('synthetic');
    expect(html).not.toContain('执行记录');
    expect(renderToStaticMarkup(<PendingApprovals conversationId="c" approvals={[]} busyId="" onDecision={async()=>true}/>)).toBe('');
  });
});
