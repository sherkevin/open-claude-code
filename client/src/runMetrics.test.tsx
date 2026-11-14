import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { ExecutionEvidenceCard, RunMetrics, buildExecutionEvidence } from './ChatPage';
import type { Run, StreamEvent } from './types';

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
  it('keeps the business scope and evidence ledger explicit without inferring missing values',()=>{
    const events:StreamEvent[]=[{id:1,run_id:'run',type:'tool.completed',created_at:'2026-11-05T10:00:01Z',data:{success:true,source_ids:['s1'],result_facts:{business_domain:'库存',process:'出库',object:'订单',metric:['数量'],time_range:'2026-11-01/2026-11-05',filters:{factory:'A'},returned_rows:3,original_rows:20,cache_hit:true}}}];
    const evidence=buildExecutionEvidence(events,{...run,usage:{input_tokens:120,output_tokens:40},timings:{first_text_ms:800,total_ms:2200}});
    expect(evidence.domain).toBe('库存');expect(evidence.process).toBe('出库');expect(evidence.returnedRows).toBe('3');expect(evidence.sourceCount).toBe('1');expect(evidence.cache).toBe('命中');expect(evidence.token).toContain('输入 120');expect(evidence.latency).toContain('首段 0.80 秒');
    const html=renderToStaticMarkup(<ExecutionEvidenceCard events={events} run={{...run,usage:null}}/>);
    expect(html).toContain('证据 1');expect(html).toContain('业务域');expect(html).toContain('未提供');
  });
  it('shows measured token stages only when the runtime reports them',()=>{
    const html=renderToStaticMarkup(<RunMetrics run={{...run,usage:{input_tokens:12,output_tokens:4,token_accounting:{stages:[{stage:'answer',source:'harness_provider'}]}}}}/>);
    expect(html).toContain('Token 阶段');expect(html).toContain('answer · harness_provider');
  });
});
