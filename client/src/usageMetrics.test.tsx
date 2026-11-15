import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { RunMetrics } from './ChatPage';
import type { Run, RunStatus } from './types';

const run: Run = {id:'usage',conversation_id:'conversation',status:'succeeded',assistant_message_id:'answer',error:null,usage:null,approvals:[],created_at:'2026-11-14T10:00:00Z',completed_at:'2026-11-14T10:00:02Z'};
const completeLedger = {
  stages: [{stage:'answer',source:'harness_provider',provider_usage:{input_tokens:12,output_tokens:4,cache_read_input_tokens:0}}],
  provider_totals: {input_tokens:12,output_tokens:4,cache_read_input_tokens:0},
  known_subtotals: {input_tokens:12,output_tokens:4,cache_read_input_tokens:0},
  provider_usage_complete:true,
  cost_is_authoritative:false,
};
const render = (changes: Partial<Run>) => renderToStaticMarkup(<RunMetrics run={{...run,...changes}}/>);

describe('provider usage completeness', () => {
  it('prefers complete ledger totals over conflicting flat counters after success', () => {
    const html = render({usage:{input_tokens:99,output_tokens:99,cache_read_input_tokens:99,estimated_cost_usd:0.25,token_accounting:completeLedger}});
    expect(html).toContain('<dt>输入 Token</dt><dd>12</dd>');
    expect(html).toContain('<dt>输出 Token</dt><dd>4</dd>');
    expect(html).toContain('<dt>缓存读取 Token</dt><dd>0</dd>');
    expect(html).toContain('<dt>费用估算</dt><dd>$0.2500</dd>');
    expect(html).not.toContain('账单');
    expect(html).not.toContain('<dd>99</dd>');
  });
  it('shows an incomplete ledger as a known subtotal even after a successful answer', () => {
    const html = render({usage:{input_tokens:12,output_tokens:4,token_accounting:{...completeLedger,provider_totals:{input_tokens:null,output_tokens:null,cache_read_input_tokens:null},provider_usage_complete:false,stages:[...completeLedger.stages,{stage:'retry',source:'harness_provider',provider_usage:{input_tokens:null,output_tokens:null,cache_read_input_tokens:null}}]}}});
    expect(html).toContain('<dt>输入 Token</dt><dd>已知小计 12，完整用量未提供</dd>');
    expect(html).toContain('<dt>输出 Token</dt><dd>已知小计 4，完整用量未提供</dd>');
    expect(html).toContain('<dt>缓存读取 Token</dt><dd>已知小计 0，完整用量未提供</dd>');
    expect(html).not.toContain('<dt>输入 Token</dt><dd>12</dd>');
  });
  it('does not convert an unknown provider contribution and its initialized subtotal zero into measured zero', () => {
    const html = render({usage:{input_tokens:77,output_tokens:55,token_accounting:{provider_totals:{input_tokens:null,output_tokens:null,cache_read_input_tokens:null},known_subtotals:{input_tokens:0,output_tokens:0,cache_read_input_tokens:0},provider_usage_complete:false,stages:[{stage:'answer',source:'harness_provider',provider_usage:{input_tokens:null,output_tokens:null,cache_read_input_tokens:null}}]}}});
    expect(html).toContain('<dt>输入 Token</dt><dd>未提供</dd>');
    expect(html).toContain('<dt>输出 Token</dt><dd>未提供</dd>');
    expect(html).toContain('<dt>缓存读取 Token</dt><dd>未提供</dd>');
    expect(html).not.toContain('已知小计 0');
    expect(html).not.toContain('77');
    expect(html).not.toContain('55');
  });
  it('does not use an empty ledger as proof of zero usage', () => {
    const html = render({usage:{token_accounting:{stages:[],provider_totals:{input_tokens:0,output_tokens:0,cache_read_input_tokens:0},known_subtotals:{input_tokens:0,output_tokens:0,cache_read_input_tokens:0},provider_usage_complete:true}}});
    expect(html).toContain('<dt>输入 Token</dt><dd>未提供</dd>');
    expect(html).not.toContain('<dd>0</dd>');
  });
  it('does not accept ledger totals without a corresponding reported stage contribution', () => {
    const html = render({usage:{token_accounting:{stages:[],provider_totals:{input_tokens:42},known_subtotals:{input_tokens:42}}}});
    expect(html).toContain('<dt>输入 Token</dt><dd>未提供</dd>');
  });
  it.each<RunStatus>(['queued','running','awaiting_approval','cancelling','failed','cancelled','interrupted'])('does not promote recorded stage totals to complete usage when the run is %s', status => {
    const html = render({status,usage:{input_tokens:12,estimated_cost_usd:0.25,token_accounting:completeLedger}});
    expect(html).toContain('<dt>输入 Token</dt><dd>已知小计 12，完整用量未提供</dd>');
    expect(html).toContain('<dt>费用估算</dt><dd>已知估算小计 $0.2500，完整费用未提供</dd>');
    expect(html).not.toContain('<dt>输入 Token</dt><dd>12</dd>');
  });
  it('labels legacy flat counters as subtotals when no complete ledger receipt exists', () => {
    const html = render({usage:{input_tokens:17,output_tokens:2}});
    expect(html).toContain('<dt>输入 Token</dt><dd>已知小计 17，完整用量未提供</dd>');
    expect(html).toContain('<dt>输出 Token</dt><dd>已知小计 2，完整用量未提供</dd>');
  });
  it('preserves known zero for product metadata queries without model calls', () => {
    const html = render({execution_kind:'product_metadata',model_call_count:0,usage:null});
    expect(html).toContain('<dt>输入 Token</dt><dd>0</dd>');
    expect(html).toContain('<dt>输出 Token</dt><dd>0</dd>');
    expect(html).toContain('<dt>费用估算</dt><dd>$0.0000</dd>');
    expect(html).not.toContain('已知小计');
  });
});

describe('measured duration', () => {
  it('preserves a terminal 1.6 second server measurement without flooring it to one second', () => {
    const html = render({timings:{total_ms:1600}});
    expect(html).toContain('<dt>总耗时（含等待）</dt><dd>1.60 秒</dd>');
  });
  it('labels timestamp differences as elapsed time rather than a server measurement', () => {
    const history = render({});
    const active = render({status:'running',timings:{total_ms:1600}});
    for(const html of [history,active]){
      expect(html).toContain('已用时间（按时间差）');
      expect(html).not.toContain('总耗时（含等待）');
    }
  });
});
