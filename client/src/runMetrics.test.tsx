import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { EventRow, ExecutionEvidenceCard, LiveActivity, RunMetrics, buildExecutionEvidence, buildExecutionEvidences, evidenceStagePresentation, runStagePresentation, webReadEvidenceSummary } from './ChatPage';
import type { Run, StreamEvent } from './types';

const run:Run={id:'run',conversation_id:'conversation',status:'succeeded',assistant_message_id:'answer',error:null,usage:null,approvals:[],created_at:'2026-11-05T10:00:00Z',completed_at:'2026-11-05T10:00:01Z'};
describe('execution metrics boundaries',()=>{
  it('shows model recovery only after a reported retry stage and keeps cancellation higher priority',()=>{
    const retry:StreamEvent={id:10,run_id:'run',type:'run.stage',created_at:run.created_at,data:{stage:'model_retry'}};
    const live=(events:StreamEvent[],status:'running'|'cancelling'='running')=>renderToStaticMarkup(<LiveActivity run={{...run,status}} events={events} stage="model_retry" connectionState="connected"/>);
    expect(live([retry])).toContain('模型连接中断，正在重试');
    expect(live([retry])).toContain('仍可以停止');
    expect(live([])).not.toContain('模型连接中断，正在重试');
    expect(live([retry],'cancelling')).toContain('已请求停止');
    expect(runStagePresentation(retry.data).confirmed).toBe(false);
  });
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
    expect(evidence.domain).toBe('库存');expect(evidence.process).toBe('出库');expect(evidence.returnedRows).toBe('3');expect(evidence.sourceCount).toBe('1');expect(evidence.cache).toBe('命中');expect(evidence).not.toHaveProperty('token');expect(evidence).not.toHaveProperty('latency');
    const html=renderToStaticMarkup(<ExecutionEvidenceCard events={events} run={{...run,usage:null}}/>);
    expect(html).toContain('来源 1');expect(html).toContain('业务域');expect(html).toContain('未提供');
  });
  it('keeps each completed tool result visible for multi-tool business tasks',()=>{
    const events:StreamEvent[]=[
      {id:1,run_id:'run',type:'tool.completed',created_at:'2026-11-05T10:00:01Z',data:{success:true,tool_name:'orders',source_ids:['orders-1'],result_facts:{business_domain:'销售',process:'交付',object:'订单',returned_rows:2}}},
      {id:2,run_id:'run',type:'tool.completed',created_at:'2026-11-05T10:00:02Z',data:{success:true,tool_name:'inventory',source_ids:['inventory-1'],result_facts:{business_domain:'库存',process:'异常',object:'物料',returned_rows:4}}},
    ];
    expect(buildExecutionEvidences(events,run)).toHaveLength(2);
    const html=renderToStaticMarkup(<ExecutionEvidenceCard events={events} run={{...run,usage:{input_tokens:120,output_tokens:40},timings:{first_text_ms:800,total_ms:2200}}}/>);
    expect(html).toContain('orders');expect(html).toContain('inventory');expect(html).toContain('销售');expect(html).toContain('库存');expect(html).not.toContain('Token');expect(html).not.toContain('首段');
  });
  it('uses measured aliases when canonical fields are unknown without converting zero or false to missing',()=>{
    const events:StreamEvent[]=[{id:1,run_id:'run',type:'tool.completed',created_at:run.created_at,data:{success:true,source_ids:[],result_facts:{domain:'unknown',business_domain:'库存',process:' UNKNOWN ',business_process:'领料',object:null,business_object:'订单',metric:'unknown',metrics:['数量'],time_range:'unknown',timeRange:'本周',returned_rows:'unknown',row_count:0,cache_hit:false}}}];
    const evidence=buildExecutionEvidence(events,run);
    expect(evidence.domain).toBe('库存');expect(evidence.process).toBe('领料');expect(evidence.object).toBe('订单');expect(evidence.metric).toBe('数量');expect(evidence.timeRange).toBe('本周');expect(evidence.returnedRows).toBe('0');expect(evidence.sourceCount).toBe('0');expect(evidence.cache).toBe('未命中');expect(evidence.originalRows).toBe('未提供');
  });
  it('separates successful, failed and unconfirmed calls and suppresses stale facts from failed results',()=>{
    const events:StreamEvent[]=[
      {id:1,run_id:'run',type:'tool.completed',created_at:run.created_at,data:{tool_name:'orders',success:true,source_ids:['s1'],result_facts:{business_domain:'销售'}}},
      {id:2,run_id:'run',type:'tool.completed',created_at:run.created_at,data:{tool_name:'inventory',success:false,source_ids:['stale-source'],result_facts:{business_domain:'不能显示的失败业务域',row_count:999,cache_hit:true}}},
      {id:3,run_id:'run',type:'tool.completed',created_at:run.created_at,data:{tool_name:'quality',source_ids:['unconfirmed-source'],result_facts:{business_domain:'不能显示的未知业务域'}}},
    ];
    const evidence=buildExecutionEvidences(events,run);
    expect(evidence.map(item=>item.status)).toEqual(['succeeded','failed','unknown']);expect(evidence[1].sourceCount).toBe('未提供');expect(evidence[2].domain).toBe('未提供');
    const html=renderToStaticMarkup(<ExecutionEvidenceCard events={events} run={run}/>);
    expect(html).toContain('orders');expect(html).toContain('inventory');expect(html).toContain('quality');expect(html).toContain('调用成功');expect(html).toContain('调用失败');expect(html).toContain('结果未确认');expect(html).not.toContain('不能显示');expect(html).not.toContain('999');
    const failedEvent=renderToStaticMarkup(<EventRow event={events[1]}/>);
    expect(failedEvent).not.toContain('来源已登记');expect(failedEvent).not.toContain('已登记 1');
  });
  it('labels call-preparation stages as records rather than successful scope enforcement',()=>{
    for(const stage of ['scoping','routing']){
      const presentation=runStagePresentation({stage});
      expect(presentation.confirmed).toBe(false);expect(presentation.detail).toContain('调用前阶段记录');
    }
    const event:StreamEvent={id:1,run_id:'run',type:'run.stage',created_at:run.created_at,data:{stage:'scoping'}};
    const html=renderToStaticMarkup(<EventRow event={event}/>);
    expect(html).toContain('记录调用范围');expect(html).not.toContain('范围限定');expect(html).not.toContain('业务漏斗阶段已记录');expect(html).not.toContain('lucide-circle-check');
  });
  it('keeps missing evidence neutral in the event log and live activity',()=>{
    const event:StreamEvent={id:1,run_id:'run',type:'run.stage',created_at:run.created_at,data:{stage:'evidence',status:'unknown',source_count:0}};
    const eventHtml=renderToStaticMarkup(<EventRow event={event}/>);
    const liveHtml=renderToStaticMarkup(<LiveActivity run={{...run,status:'running'}} events={[event]} stage="evidence" connectionState="connected"/>);
    for(const html of [eventHtml,liveHtml]){expect(html).toContain('来源未提供');expect(html).not.toContain('lucide-circle-check');expect(html).not.toContain('已登记 0');}
    expect(runStagePresentation({stage:'evidence',status:'reported',source_count:2})).toEqual({label:'来源已登记',detail:'已登记 2 个来源，内容仍需核对。',confirmed:true});
    expect(runStagePresentation({stage:'evidence',status:'reported',source_count:0}).confirmed).toBe(false);
  });
  it('shows a failed webpage read ahead of the generic missing-source warning', () => {
    const events:StreamEvent[] = [
      {id:1,run_id:'run',type:'tool.completed',created_at:run.created_at,data:{tool_name:'WebSearch',success:true,source_ids:['candidate']}},
      {id:2,run_id:'run',type:'run.stage',created_at:run.created_at,data:{stage:'evidence',tool_name:'mcp__workspace__web_read',status:'unknown',source_count:0}},
      {id:3,run_id:'run',type:'tool.completed',created_at:run.created_at,data:{tool_name:'mcp__workspace__web_read',success:false,error_code:'WEB_TIMEOUT',error_message:'网页读取超时，请尝试其他来源',source_ids:[]}},
    ];
    const live = renderToStaticMarkup(<LiveActivity run={{...run,status:'running'}} events={events} stage="evidence" connectionState="connected"/>);
    const row = renderToStaticMarkup(<EventRow event={events[1]} events={events}/>);
    for (const html of [live,row]) {
      expect(html).toContain('网页读取未完成'); expect(html).toContain('网页读取超时');
      expect(html).not.toContain('不能据此确认业务结论'); expect(html).not.toContain('已登记 1 个来源');
    }
  });
  it('keeps earlier trusted source count when later pages fail without counting search candidates', () => {
    const events:StreamEvent[] = [
      {id:1,run_id:'run',type:'tool.completed',created_at:run.created_at,data:{tool_name:'mcp__workspace__web_read',success:true,source_ids:['read-1','read-1']}},
      {id:2,run_id:'run',type:'tool.completed',created_at:run.created_at,data:{tool_name:'mcp__workspace__web_search',success:true,source_ids:['candidate-1','candidate-2']}},
      {id:3,run_id:'run',type:'run.stage',created_at:run.created_at,data:{stage:'evidence',tool_name:'web_read',status:'unknown',source_count:0}},
      {id:4,run_id:'run',type:'tool.completed',created_at:run.created_at,data:{tool_name:'web_read',success:false,error_code:'WEB_FETCH_FAILED',error_message:'网页返回 HTTP 403，可尝试其他来源'}},
    ];
    const live = renderToStaticMarkup(<LiveActivity run={{...run,status:'running'}} events={events} stage="evidence" connectionState="connected"/>);
    expect(live).toContain('部分网页读取未完成'); expect(live).toContain('已登记 1 个来源'); expect(live).toContain('HTTP 403');
    expect(live).not.toContain('已登记 3 个来源'); expect(live).not.toContain('来源未提供');
    const card = renderToStaticMarkup(<ExecutionEvidenceCard run={run} events={events}/>);
    expect(card).toContain('部分网页读取未完成'); expect(card).toContain('本轮已登记 1 个来源');
  });
  it('never borrows source counts or failure reasons from a different run', () => {
    const event = (id:number,run_id:string,success:boolean):StreamEvent => ({id,run_id,type:'tool.completed',created_at:run.created_at,data:{tool_name:'web_read',success,source_ids:success?['s1']:[],error_message:'失败原因'}});
    expect(webReadEvidenceSummary([event(1,'foreign',false),event(2,'run',true)],'run')).toBeNull();
    const result = webReadEvidenceSummary([event(1,'foreign',true),event(2,'run',false)],'run');
    expect(result?.label).toBe('网页读取未完成'); expect(result?.detail).not.toContain('已登记');
  });
  it('keeps unconfirmed reads and missing error details unknown', () => {
    const unknown:StreamEvent = {id:1,run_id:'run',type:'tool.completed',created_at:run.created_at,data:{tool_name:'web_read',source_ids:['unconfirmed']}};
    expect(webReadEvidenceSummary([unknown],'run')).toBeNull();
    const failed:StreamEvent = {...unknown,id:2,data:{tool_name:'web_read',success:false,source_ids:['failed-source'],error:{detail:'private-body'},tool_response:'private-provider-body'}};
    const summary = webReadEvidenceSummary([unknown,failed],'run');
    expect(summary?.label).toBe('网页读取未完成'); expect(summary?.detail).toContain('具体原因未提供');
    expect(summary?.detail).not.toContain('private'); expect(summary?.detail).not.toContain('已登记');
  });
  it('does not relabel an earlier evidence stage with a later read failure', () => {
    const stage:StreamEvent = {id:1,run_id:'run',type:'run.stage',created_at:run.created_at,data:{stage:'evidence',tool_name:'web_read',status:'reported',source_count:1}};
    const events:StreamEvent[] = [stage,
      {id:2,run_id:'run',type:'tool.completed',created_at:run.created_at,data:{tool_name:'web_read',success:true,source_ids:['s1']}},
      {...stage,id:3,data:{stage:'evidence',tool_name:'web_read',status:'unknown',source_count:0}},
      {id:4,run_id:'run',type:'tool.completed',created_at:run.created_at,data:{tool_name:'web_read',success:false,error_message:'网页读取超时'}},
    ];
    expect(evidenceStagePresentation(stage,events)).toEqual(runStagePresentation(stage.data));
    expect(evidenceStagePresentation(events[2],events).label).toBe('部分网页读取未完成');
    expect(evidenceStagePresentation(stage,[stage,events[2],events[3]])).toEqual(runStagePresentation(stage.data));
  });
  it('reports anonymous source counts as a lower bound and rejects fractional counts', () => {
    const failed:StreamEvent = {id:2,run_id:'run',type:'tool.completed',created_at:run.created_at,data:{tool_name:'web_read',success:false}};
    const succeeded:StreamEvent = {id:1,run_id:'run',type:'tool.completed',created_at:run.created_at,data:{tool_name:'web_read',success:true,result_facts:{source_count:2}}};
    expect(webReadEvidenceSummary([succeeded,failed],'run')?.detail).toContain('至少 2 个来源');
    expect(webReadEvidenceSummary([{...succeeded,data:{...succeeded.data,result_facts:{source_count:1.5}}},failed],'run')?.label).toBe('网页读取未完成');
  });
  it('shows measured token stages only when the runtime reports them',()=>{
    const html=renderToStaticMarkup(<RunMetrics run={{...run,usage:{input_tokens:12,output_tokens:4,token_accounting:{stages:[{stage:'answer',source:'harness_provider',provider_usage:{input_tokens:12,output_tokens:4,cache_read_input_tokens:2}}]}}}}/>);
    expect(html).toContain('Token 阶段');expect(html).toContain('answer</strong> · harness_provider');expect(html).toContain('入 12 · 出 4 · 缓 2');
  });
});
