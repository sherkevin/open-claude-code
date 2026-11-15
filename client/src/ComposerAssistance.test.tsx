import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { ComposerCompletion, ComposerFollowups, completedAnswerRun, completionNotice, completionSuggestion, composerInputMaxHeight, composerShortcut, followupTexts } from './ComposerAssistance';
import type { Message, Run } from './types';

const state = { composing:false, draft:'查询本周订单', canComplete:true, completionBusy:false, suggestion:false, skillsOpen:false };
const run: Run = { id:'r1',conversation_id:'c1',status:'succeeded',kind:'chat',assistant_message_id:'a1',error:null,usage:null,approvals:[],created_at:'2026-11-15T00:00:00Z',completed_at:'2026-11-15T00:00:01Z' };
const answer = { id:'a1',conversation_id:'c1',role:'assistant',run_id:'r1',text:'已按范围汇总。',file_ids:[],created_at:'2026-11-15T00:00:01Z' } as Message;

describe('completion preserves the draft and explains unavailable responses', () => {
  it('accepts only a bounded extension of the exact original draft', () => {
    const draft = '  查询订单\n  按客户  ';
    expect(completionSuggestion(draft, draft + '并列出来源')).toBe(draft + '并列出来源');
    expect(completionSuggestion(draft, draft.trim() + '并列出来源')).toBe('');
    expect(completionSuggestion(draft, '删除订单')).toBe('');
    expect(completionSuggestion(draft, draft)).toBe('');
    expect(completionSuggestion(draft, draft + '  ')).toBe('');
    expect(completionSuggestion(draft, null)).toBe('');
    expect(completionSuggestion(draft, draft + '质'.repeat(701))).toBe('');
    expect(completionSuggestion('😀'.repeat(499), '😀'.repeat(500))).toBe('😀'.repeat(500));
  });
  it.each(['unavailable', 'provider_unconfigured', 'provider_invalid', 'budget_disabled'])('reports %s as an unavailable configuration', mode => {
    expect(completionNotice(mode)).toContain('管理员');
    expect(completionNotice(mode)).not.toContain('没有更合适');
  });
  it('distinguishes disabled, invalid output and a long draft', () => {
    expect(completionNotice('disabled')).toContain('关闭');
    expect(completionNotice('provider_response_rejected')).toContain('有效补全');
    expect(completionNotice('input_too_long')).toContain('500');
  });
});

describe('composer shortcuts respect focus and IME',()=>{
  it('uses a plain Tab to request, then explicitly accept a preview',()=>{
    expect(composerShortcut({key:'Tab'},state)).toBe('complete');
    expect(composerShortcut({key:'Tab'},{...state,suggestion:true})).toBe('accept');
  });
  it.each([{shiftKey:true},{ctrlKey:true},{metaKey:true},{altKey:true},{repeat:true},{isComposing:true},{keyCode:229}])('does not capture modified Tab or IME: %j',change=>{
    expect(composerShortcut({key:'Tab',...change},state)).toBe('none');
  });
  it.each([{draft:''},{draft:'  '},{composing:true},{completionBusy:true},{canComplete:false},{skillsOpen:true}])('keeps normal focus navigation when completion is ineligible: %j',change=>{
    expect(composerShortcut({key:'Tab'},{...state,...change})).toBe('none');
  });
  it('keeps Shift Enter and IME confirmation from sending',()=>{
    expect(composerShortcut({key:'Enter'},state)).toBe('send');
    expect(composerShortcut({key:'Enter',ctrlKey:true},state)).toBe('send');
    expect(composerShortcut({key:'Enter',shiftKey:true},state)).toBe('none');
    expect(composerShortcut({key:'Enter',isComposing:true},state)).toBe('none');
    expect(composerShortcut({key:'Enter',keyCode:229},state)).toBe('none');
    expect(composerShortcut({key:'Enter'},{...state,skillsOpen:true})).toBe('none');
  });
});

describe('followups only use a finished current answer',()=>{
  it('requires the last message and run to match the current conversation',()=>{
    expect(completedAnswerRun('c1',run,[answer])).toBe('r1');
    expect(completedAnswerRun('c2',run,[answer])).toBeNull();
    expect(completedAnswerRun('c1',run,[{...answer,run_id:'older'}])).toBeNull();
    expect(completedAnswerRun('c1',run,[answer,{...answer,id:'u2',role:'user',text:'下一题'}])).toBeNull();
    expect(completedAnswerRun('c1',run,[{...answer,text:'  '}])).toBeNull();
    expect(completedAnswerRun('c1',{...run,kind:'compact'},[answer])).toBeNull();
  });
  it.each(['running','queued','failed','cancelled','interrupted','awaiting_approval','cancelling'] as const)('does not promote %s into a completed answer',status=>{
    expect(completedAnswerRun('c1',{...run,status},[answer])).toBeNull();
  });
  it('bounds and deduplicates plain-text suggestions without inventing fallback questions',()=>{
    expect(followupTexts([' 查库存 ', '查库存', null, '按客户比较', '分两\n行', 'x'.repeat(161), '看明细', '第四题'])).toEqual(['查库存','按客户比较','看明细']);
    expect(followupTexts(undefined)).toEqual([]);
    expect(followupTexts([])).toEqual([]);
  });
  it('uses non-submitting buttons and announces pending states',()=>{
    const suggestions=renderToStaticMarkup(<ComposerFollowups value={{busy:false,suggestions:['看看明细'],notice:'',runId:'r1'}} onChoose={()=>{}}/>);
    expect(suggestions).toContain('type="button"');expect(suggestions).not.toContain('type="submit"');
    const pending=renderToStaticMarkup(<ComposerCompletion value={{busy:true,suggestion:'',notice:''}} onAccept={()=>{}} onDismiss={()=>{}}/>);
    expect(pending).toContain('role="status"');expect(pending).toContain('正在补全');expect(pending).toContain('取消补全');
  });
});


it('keeps long followup suggestions collapsed by default and accessible for explicit expansion',()=>{
 const html=renderToStaticMarkup(<ComposerFollowups value={{busy:false,suggestions:['按客户核对本月发货数量和关联订单金额，并说明范围。','核对尚未发货的订单'],notice:'',runId:'r1'}} onChoose={()=>{}}/>);
 expect(html).toContain('<details');expect(html).toContain('<summary>');expect(html).not.toContain(' open=');
 expect(html).toContain('2 个问题');expect(html).toContain('按客户核对本月发货数量和关联订单金额，并说明范围。');
});
it('bounds long drafts for short viewports without changing draft content',()=>{
 expect(composerInputMaxHeight(900)).toBe(144);
 expect(composerInputMaxHeight(720)).toBe(130);
 expect(composerInputMaxHeight(400)).toBe(72);
 expect(composerInputMaxHeight(300)).toBe(72);
});
