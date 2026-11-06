import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { RunMetrics } from './ChatPage';
import type { Run } from './types';

const run:Run={id:'run',conversation_id:'conversation',status:'succeeded',assistant_message_id:'answer',error:null,usage:null,approvals:[],created_at:'2026-11-05T10:00:00Z',completed_at:'2026-11-05T10:00:01Z'};
describe('execution metrics boundaries',()=>{
  it('labels product metadata answers as a model information query with zero model usage',()=>{
    const html=renderToStaticMarkup(<RunMetrics run={{...run,execution_kind:'product_metadata',identity_source:'run_selection',model_call_count:0,usage:{source:'product_metadata',input_tokens:0,output_tokens:0,cache_read_input_tokens:0,estimated_cost_usd:0}}}/>);
    expect(html).toContain('模型信息查询');expect(html).toContain('未调用模型');expect(html).toContain('<dt>输入 Token</dt><dd>0</dd>');expect(html).toContain('<dt>输出 Token</dt><dd>0</dd>');expect(html).toContain('$0.0000');expect(html).not.toContain('未提供');
  });
  it('does not infer zero cost or zero calls for ordinary model runs when usage is unavailable',()=>{
    const html=renderToStaticMarkup(<RunMetrics run={{...run,execution_kind:'model',identity_source:null,model_call_count:null}}/>);
    expect(html).not.toContain('未调用模型');expect(html).not.toContain('模型信息查询');expect(html).toContain('<dt>输入 Token</dt><dd>未提供</dd>');expect(html).toContain('<dt>费用估算</dt><dd>未提供</dd>');expect(html).not.toContain('$0.0000');
  });
});
