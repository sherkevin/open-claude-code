import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import BusinessResults, { BusinessResultCard, csvCell, displayBusinessCell, displayBusinessTime, parseBusinessResult, preferredChartMetric, questionForRun, resultCSV, resultsFromEvents } from './BusinessResult';
import type { Message, StreamEvent } from './types';
const sample=()=>({schema_version:'business-result/v1',title:'发货汇总',columns:[{key:'customer',label:'客户'},{key:'amount',label:'金额',unit:'CNY'}],rows:[{customer:'甲厂',amount:1200},{customer:'乙厂',amount:2400}],scope:{region:'华北',date:'2026-11-14'},complete:true,synthetic:true,truncated:false,total_rows:2,warnings:[],data_time:'2026-11-14T10:00:00+08:00'});
const event=(success:unknown=true,run='run-1'):StreamEvent=>({id:1,run_id:run,type:'tool.completed',created_at:'2026-11-14',data:{success,business_result:sample(),source_ids:['src_1']}});
describe('bounded business results',()=>{
 it('accepts typed results and leaves unreported coverage unknown',()=>{const r=parseBusinessResult({...sample(),complete:undefined,total_rows:undefined})!;expect(r.complete).toBeNull();expect(r.total_rows).toBeNull();expect(r.returned_rows).toBe(2);});
 it('does not render failed, unknown, old-run or malformed results',()=>{expect(resultsFromEvents([event(false),event('true'),event(true,'old')],'run-1')).toEqual([]);expect(parseBusinessResult({...sample(),rows:[{customer:{html:'bad'},amount:2}]})).toBeNull();});
 it('uses source ids without allowing a URL as an ID',()=>{const e=event();e.data.source_ids=['src_1','https://evil.invalid'];expect(resultsFromEvents([e],'run-1')[0].result.source_ids).toEqual(['src_1']);});
 it('hides cancelled/interrupted tasks and ignores success after a terminal event',()=>{for(const status of ['cancelled','cancelling','interrupted'] as const)expect(resultsFromEvents([event()],'run-1',status)).toEqual([]);const done={...event(),id:1,type:'run.completed',data:{status:'succeeded'}};const late={...event(),id:2};expect(resultsFromEvents([done,late],'run-1','succeeded')).toEqual([]);});
 it('preserves successful tool data before model failure and labels it as an incomplete answer',()=>{
   const done:StreamEvent={...event(),id:5,type:'run.completed',data:{status:'failed'}};
   const events=[event(),{...event(false),id:2},{...event('unknown'),id:3},done,{...event(),id:6}];
   expect(resultsFromEvents(events,'run-1','failed').map(item=>item.event.id)).toEqual([1]);
   const html=renderToStaticMarkup(<BusinessResults events={events} runId="run-1" status="failed"/>);
   expect(html).toContain('业务数据已返回，回答未完成');expect(html).toContain('甲厂');expect(html).toContain('查看依据 1');
   expect(html).not.toContain('任务已成功');
   expect(resultCSV(resultsFromEvents(events,'run-1','failed')[0].result,'run-1',1,true)).toContain('业务数据已返回，回答未完成');
 });
 it('does not resurrect tool data from an unknown, cancelled or interrupted terminal event',()=>{
   for(const status of ['unknown','cancelled','interrupted']){
     const done:StreamEvent={...event(),id:5,type:'run.completed',data:{status}};
     expect(resultsFromEvents([event(),done],'run-1','failed')).toEqual([]);
   }
 });
 it('uses the earliest terminal event even if events arrive out of order',()=>{
   const done:StreamEvent={...event(),id:3,type:'run.completed',data:{status:'failed'}};
   const later:StreamEvent={...done,id:7};
   expect(resultsFromEvents([later,{...event(),id:4},done,event()],'run-1','failed').map(item=>item.event.id)).toEqual([1]);
 });
 it('never trusts source IDs supplied inside a business payload',()=>{const e=event();(e.data.business_result as Record<string,unknown>).source_ids=['other_private_source'];expect(resultsFromEvents([e],'run-1')[0].result.source_ids).toEqual(['src_1']);e.data.source_ids=undefined;expect(resultsFromEvents([e],'run-1')[0].result.source_ids).toEqual([]);});
 it('rejects ambiguous columns and nonfinite values',()=>{expect(parseBusinessResult({...sample(),columns:[{key:'x',label:'x'},{key:'x',label:'duplicate'}]})).toBeNull();expect(parseBusinessResult({...sample(),rows:[{customer:'甲',amount:NaN}]})).toBeNull();});
 it('does not silently truncate excess data',()=>{expect(parseBusinessResult({...sample(),rows:Array.from({length:201},()=>({customer:'甲',amount:1}))})).toBeNull();});
 it('neutralizes spreadsheet formulas in text, preserving real numbers',()=>{for(const value of ['=HYPERLINK("x")',' +1','\t=1','-formula','@SUM(1)','\nfoo'])expect(csvCell(value).startsWith('"\'')).toBe(true);expect(csvCell(-2)).toBe('"-2"');expect(csvCell('a,"b')).toBe('"a,""b"');});
 it('exports actual rows with scope units partial notice and task identity',()=>{const r=parseBusinessResult({...sample(),complete:false})!;const csv=resultCSV(r,'run-1',8);for(const v of ['run-1','华北','金额（CNY）','部分结果','"乙厂","2400"','示例数据'])expect(csv).toContain(v);});
 it('exports only host-registered source IDs and still neutralizes formulas',()=>{const e=event();(e.data.business_result as Record<string,unknown>).source_ids=['untrusted_source'];e.data.source_ids=['src_1','src_2','=HYPERLINK("x")'];const r=resultsFromEvents([e],'run-1')[0].result;r.rows[0].customer='=HYPERLINK("x")';const csv=resultCSV(r,'run-1',1);expect(csv).toContain('"来源 ID","src_1"');expect(csv).toContain('"来源 ID","src_2"');expect(csv).not.toContain('untrusted_source');expect(csv).toContain('"\'=HYPERLINK');});
 it('shows synthetic identity and escaped untrusted text',()=>{const r=parseBusinessResult({...sample(),title:'<script>bad</script>',complete:undefined})!;const html=renderToStaticMarkup(<BusinessResultCard result={r} runId="run-1" eventId={1}/>);expect(html).toContain('示例数据');expect(html).toContain('完整性未说明');expect(html).toContain('&lt;script&gt;');expect(html).not.toContain('<script>');expect(html).toContain('导出当前结果');});
 it('does not silently round a very small nonzero business value to zero',()=>{const r=parseBusinessResult({...sample(),rows:[{customer:'甲',amount:4e-23}]})!;const html=renderToStaticMarkup(<BusinessResultCard result={r} runId="r" eventId={1}/>);expect(html).toContain('4e-23');expect(resultCSV(r,'r',1)).toContain('4e-23');});
 it('never labels tool declarations as verified customer data',()=>{expect(resultCSV(parseBusinessResult({...sample(),synthetic:false})!,'r',1)).toContain('数据源标记为非示例');});
 it('keeps the actual range visible for an empty result without claiming a zero business total',()=>{const r=parseBusinessResult({...sample(),rows:[],total_rows:0,scope:{organization:'演示验收组织',region:'华北',customer:'演示客户',status:'完成',date_from:'2026-11-01',date_to:'2026-11-14'}})!;const html=renderToStaticMarkup(<BusinessResultCard result={r} runId="empty" eventId={2}/>);expect(html).toContain('class="br-evidence" open=""');const primary=html.slice(html.indexOf('aria-label="主要查询条件"'),html.indexOf('class="br-summary"'));expect(primary).toContain('开始日期');expect(primary).toContain('结束日期');expect(html).toContain('本次查询未返回记录');expect(html).not.toContain('金额为零');});
});

describe('chart metric follows the current question without changing data',()=>{
 const columns=[{key:'records',label:'记录数'},{key:'quantity',label:'数量',unit:'件'},{key:'amount',label:'金额',unit:'CNY'}];
 it('chooses the explicitly requested quantity or amount instead of the first record-count column',()=>{
   expect(preferredChartMetric(columns,'查 10 万条记录中各客户的发货数量')).toBe('quantity');
   expect(preferredChartMetric(columns,'请按客户比较金额')).toBe('amount');
   expect(preferredChartMetric(columns,'返回记录数')).toBe('records');
 });
 it('keeps column order when no single metric is specified',()=>{
   expect(preferredChartMetric(columns,'列出各客户')).toBe('records');
   expect(preferredChartMetric(columns,'金额和数量都看一下')).toBe('records');
   expect(preferredChartMetric([],'查金额')).toBe('');
 });
 it('prefers a specific returned field over an overlapping shorter label',()=>{
   expect(preferredChartMetric([{key:'all',label:'金额'},{key:'open',label:'未结金额（元）'}],'比较未结金额')).toBe('open');
 });
 it('matches a uniquely identified amount field without assuming that amount always means quantity',()=>{
   const reported=[columns[0],{key:'order_amount',label:'关联订单金额',unit:'CNY'},{key:'shipped_quantity',label:'发货数量',unit:'件'}];
   expect(preferredChartMetric(reported,'比较各客户金额')).toBe('order_amount');
   expect(preferredChartMetric(reported,'比较各客户数量')).toBe('shipped_quantity');
   expect(preferredChartMetric(reported,'看金额和数量')).toBe('records');
 });
 it('keeps the original default when a short amount term could refer to more than one field',()=>{
   const reported=[columns[0],{key:'orders',label:'关联订单金额'},{key:'paid',label:'已收金额'}];
   expect(preferredChartMetric(reported,'比较金额')).toBe('records');
   expect(preferredChartMetric(reported,'比较已收金额')).toBe('paid');
 });
 it('does not select an explicitly excluded or sorting-only metric',()=>{
   const reported=[columns[0],{key:'orders',label:'关联订单金额'},{key:'shipped',label:'发货数量'}];
   expect(preferredChartMetric(reported,'不要金额')).toBe('records');
   expect(preferredChartMetric(reported,'不看关联订单金额')).toBe('records');
   expect(preferredChartMetric(reported,'按金额降序，展示数量')).toBe('shipped');
   expect(preferredChartMetric(columns,'不看金额，只看数量')).toBe('quantity');
   expect(preferredChartMetric(columns,'按金额排序，展示数量')).toBe('quantity');
 });
 it('never takes a newer or assistant-written question from another run',()=>{
   const message=(run_id:string,role:'user'|'assistant',text:string):Message=>({id:text,run_id,role,text,conversation_id:'c',file_ids:[],created_at:''});
   const messages=[message('old','user','数量'),message('old','assistant','金额'),message('new','user','金额')];
   expect(questionForRun(messages,'old')).toBe('数量');expect(questionForRun(messages,'missing')).toBe('');
 });
 it('uses an explicit how-many unit only when the returned numeric column is unambiguous',()=>{
   const returned=[{key:'records',label:'发货记录数',unit:'笔'},{key:'quantity',label:'发货数量',unit:'件'},{key:'amount',label:'订单金额',unit:'元'}];
   expect(preferredChartMetric(returned,'2026-11-14 各客户发货多少件')).toBe('quantity');
   expect(preferredChartMetric(returned,'各客户发货几件？')).toBe('quantity');
   expect(preferredChartMetric(returned,'订单一共多少元？')).toBe('amount');
   expect(preferredChartMetric(returned,'不要查多少件，只看订单金额')).toBe('amount');
   expect(preferredChartMetric(returned,'不要查多少件')).toBe('records');
   expect(preferredChartMetric(returned,'按多少件降序')).toBe('records');
   expect(preferredChartMetric(returned,'订单金额和发货多少件都看一下')).toBe('records');
   expect(preferredChartMetric([...returned,{key:'stock',label:'库存数量',unit:'件'}],'发货多少件')).toBe('records');
   expect(preferredChartMetric(returned,'有多少箱？')).toBe('records');
   expect(preferredChartMetric(returned,'有多少万元？')).toBe('records');
 });
});

it('attributes result completeness and totals to the data source in the card and CSV',()=>{
 const result=parseBusinessResult(sample())!;
 const html=renderToStaticMarkup(<BusinessResultCard result={result} runId="r" eventId={1}/>);
 expect(html).toContain('数据源报告共 2 行');expect(html).toContain('数据源标记为完整');
 expect(html).not.toContain('工具声明完整');
 const csv=resultCSV(result,'r',1);
 expect(csv).toContain('数据源报告总行数');expect(csv).toContain('数据源标记为完整');
});

it('formats explicit monetary units with two minimum decimals without changing scale or losing source precision',()=>{
 const money={key:'amount',label:'金额',unit:'CNY'};
 expect(displayBusinessCell(7112374.3,money)).toBe('7,112,374.30');
 expect(displayBusinessCell(0,money)).toBe('0.00');
 expect(displayBusinessCell(-12.5,money)).toBe('-12.50');
 expect(displayBusinessCell(12345,{...money,unit:'分'})).toBe('12,345');
 expect(displayBusinessCell(1.2345,money)).toBe('1.2345');
 expect(displayBusinessCell(null,money)).toBe('—');
 expect(displayBusinessCell(0.0000001,money)).toBe('1e-7');
 expect(displayBusinessCell(12.5,{key:'quantity',label:'数量',unit:'吨'})).toBe('12.5');
 const data=parseBusinessResult({...sample(),rows:[{customer:'甲厂',amount:7112374.3}]})!;
 expect(renderToStaticMarkup(<BusinessResultCard result={data} runId="money" eventId={1}/>)).toContain('7,112,374.30');
 expect(resultCSV(data,'money',1)).toContain('"7112374.3"');
});


it('presents source data time without browser timezone conversion or loss of precision',()=>{
 expect(displayBusinessTime('2026-11-14T12:00:00+08:00')).toBe('2026-11-14 12:00:00（UTC+08:00）');
 expect(displayBusinessTime('2026-11-14T23:59:59.123456-05:30')).toBe('2026-11-14 23:59:59.123456（UTC-05:30）');
 expect(displayBusinessTime('2026-11-14T12:00:00Z')).toBe('2026-11-14 12:00:00（UTC）');
 expect(displayBusinessTime('2026-11-14T12:00:00')).toBe('2026-11-14 12:00:00');
 expect(displayBusinessTime('2026-11-14T12:00:00-00:00')).toContain('时区未明确');
 expect(displayBusinessTime('2026-11-14')).toBe('2026-11-14');
 expect(displayBusinessTime('以发货日期为准')).toBe('以发货日期为准');
 expect(displayBusinessTime(null)).toBe('未提供');
 const result=parseBusinessResult(sample())!;
 const html=renderToStaticMarkup(<BusinessResultCard result={result} runId="time" eventId={1}/>);
 expect(html).toContain('数据时间：2026-11-14 10:00:00（UTC+08:00）');
 expect(html).toContain('title="2026-11-14T10:00:00+08:00"');
 expect(resultCSV(result,'time',1)).toContain('"数据时间","2026-11-14T10:00:00+08:00"');
});
