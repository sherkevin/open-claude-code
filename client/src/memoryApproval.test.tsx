import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import MemoryApprovalCard, { MEMORY_WRITE_TOOL, MemoryReviewBody, validateMemoryReview } from './MemoryApprovalCard';
import { eventTypes } from './stream';
import { toolLabel } from './display';
import type { Approval, MemoryApprovalContent } from './types';

const review:MemoryApprovalContent={conversation_id:'session-one',old_content_sha256:null,path:'MEMORY.md',content:'private note',expected_revision:2,native_epoch:3,operation_id:'op-one',content_digest:'a'.repeat(64)};
const approval:Approval={id:'approval-one',run_id:'run-one',tool_name:MEMORY_WRITE_TOOL,arguments:{path:'MEMORY.md'},status:'pending',created_at:'2026-01-01T00:00:00Z',expires_at:'2099-01-01T00:00:00Z'};
describe('controlled memory review',()=>{
  it('requires complete body, bounded UTF-8 size, revision, epoch, operation and digest',()=>{
    expect(validateMemoryReview(review)).toBe(review);
    expect(()=>validateMemoryReview(review,'different-session')).toThrow('正文不完整');
    for(const patch of [{content:undefined},{content:'中'.repeat(45000)},{expected_revision:-1},{native_epoch:NaN},{operation_id:''},{content_digest:'preview'}]) {
      expect(()=>validateMemoryReview({...review,...patch} as MemoryApprovalContent)).toThrow('正文不完整');
    }
  });
  it('renders the full body beyond log truncation, without interpreting HTML or Markdown links',()=>{
    const body='a'.repeat(20000)+'<img src="https://not-requested.invalid/x"> [link](https://not-requested.invalid/y) END-OF-FULL-CONTENT';
    const html=renderToStaticMarkup(<MemoryReviewBody value={{...review,content:body}}/>);
    expect(html).toContain('END-OF-FULL-CONTENT');expect(html).toContain('&lt;img');expect(html).not.toContain('<img');expect(html).not.toContain('<a ');
    expect(html).toContain('批准仅将内容暂存');expect(html).toContain('失败或取消不会写入');
  });
  it('does not allow approval from an SSE preview before full content is loaded and confirmed',()=>{
    const html=renderToStaticMarkup(<MemoryApprovalCard conversationId="session-one" approval={{...approval,arguments:{content:'UNTRUSTED-PREVIEW'}}} busy={false} onDecision={async()=>true}/>);
    expect(html).not.toContain('UNTRUSTED-PREVIEW');expect(html).toMatch(/<button[^>]*disabled=""[^>]*>[\s\S]*?批准暂存/);
  });
  it('subscribes to pending, committed and aborted memory events and labels tools distinctly',()=>{
    for(const type of ['memory.pending','memory.updated','memory.aborted'])expect(eventTypes).toContain(type);
    expect(toolLabel(MEMORY_WRITE_TOOL)).toBe('申请保存会话记忆');
    expect(toolLabel('mcp__workspace__memory_read')).toBe('读取当前会话记忆');
  });
});
