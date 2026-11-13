import { describe, expect, it } from 'vitest';
import { fitnessTaskSummary } from './ChatPage';
import type { Message } from './types';

const message=(role:'user'|'assistant',text:string)=>({role,text} as unknown as Message);

describe('fitness model task summary',()=>{
  it('combines current input with recent context before routing',()=>{
    expect(fitnessTaskSummary('  分析质量报告  ',[message('user','旧任务')])).toBe('旧任务\n分析质量报告');
    expect(fitnessTaskSummary('x'.repeat(2500),[])).toHaveLength(2000);
  });
  it('uses recent user messages when the composer is empty',()=>{
    const result=fitnessTaskSummary('',[message('assistant','回答'),message('user','请比较两种工艺'),message('user','并输出表格')]);
    expect(result).toBe('请比较两种工艺\n并输出表格');
  });
  it('has a safe bounded fallback for a brand-new conversation',()=>{
    expect(fitnessTaskSummary('  ',[],'' )).toBe('根据当前会话上下文选择最合适的模型。');
  });
});
