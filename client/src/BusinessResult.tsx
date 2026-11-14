import { useMemo, useState } from 'react';
import { BarChart3, Database, Download, Table2 } from 'lucide-react';
import type { RunStatus, StreamEvent } from './types';
import { SourceDialog } from './Sources';
import './business-result.css';

type Cell = string | number | boolean | null;
export type ResultColumn = { key: string; label: string; type?: string; unit?: string };
export type BusinessResult = {
  schema_version: 'business-result/v1'; title: string; columns: ResultColumn[];
  rows: Record<string, Cell>[]; summary?: string; scope?: Record<string, unknown>;
  data_time?: string | null; returned_rows: number; total_rows: number | null;
  complete: boolean | null; synthetic: boolean | null; truncated: boolean;
  warnings: string[]; source_ids?: string[];
};
const object = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v);
const scalar = (v: unknown): v is Cell => v === null || typeof v === 'string' || typeof v === 'boolean' || (typeof v === 'number' && Number.isFinite(v));
export function parseBusinessResult(value: unknown): BusinessResult | null {
  if (!object(value) || value.schema_version !== 'business-result/v1' || !Array.isArray(value.columns) || !Array.isArray(value.rows)) return null;
  if (value.columns.length > 24 || !value.columns.length || value.rows.length > 200) return null;
  const columns: ResultColumn[] = [];
  for (const c of value.columns) {
    if (!object(c) || typeof c.key !== 'string' || !c.key || typeof c.label !== 'string' || columns.some(item => item.key === c.key)) return null;
    columns.push({ key: c.key, label: c.label, ...(typeof c.type === 'string' ? {type: c.type} : {}), ...(typeof c.unit === 'string' ? {unit: c.unit} : {}) });
  }
  const rows: Record<string, Cell>[] = [];
  for (const row of value.rows) {
    if (!object(row)) return null;
    const result: Record<string, Cell> = Object.create(null);
    for (const column of columns) {
      const cell = Object.hasOwn(row, column.key) ? row[column.key] : null;
      if (!scalar(cell)) return null;
      result[column.key] = cell;
    }
    rows.push(result);
  }
  const total = typeof value.total_rows === 'number' && Number.isSafeInteger(value.total_rows) && value.total_rows >= 0 ? value.total_rows : null;
  return {schema_version:'business-result/v1',title:typeof value.title==='string'?value.title:'业务查询结果',columns,rows,
    summary:typeof value.summary==='string'?value.summary:undefined,scope:object(value.scope)?value.scope:undefined,
    data_time:typeof value.data_time==='string'?value.data_time:null,returned_rows:rows.length,total_rows:total,
    complete:typeof value.complete==='boolean'?value.complete:null,synthetic:typeof value.synthetic==='boolean'?value.synthetic:null,
    truncated:value.truncated===true,warnings:Array.isArray(value.warnings)?value.warnings.filter((w):w is string=>typeof w==='string').slice(0,20):[],
    source_ids:Array.isArray(value.source_ids)?value.source_ids.filter((s):s is string=>typeof s==='string'&&/^[A-Za-z0-9_-]{1,100}$/.test(s)).slice(0,30):[]};
}
export function resultsFromEvents(events: StreamEvent[], runId: string, status: RunStatus = 'succeeded') {
  if (['cancelling','cancelled','failed','interrupted'].includes(status)) return [];
  const terminal = events.find(e=>e.run_id===runId&&e.type==='run.completed');
  return events.filter(e => e.run_id===runId && e.type==='tool.completed' && e.data.success===true && (!terminal||e.id<terminal.id)).flatMap(event => {
    const result=parseBusinessResult(event.data.business_result);
    if (!result) return [];
    // Only the host's registered references are trusted, never tool-supplied IDs.
    result.source_ids=Array.isArray(event.data.source_ids)?[...new Set(event.data.source_ids.filter((id):id is string=>typeof id==='string'&&/^[A-Za-z0-9_-]{1,100}$/.test(id)))].slice(0,30):[];
    return [{event,result}];
  });
}
export function csvCell(value: unknown): string {
  let text=value===null||value===undefined?'':typeof value==='object'?JSON.stringify(value):String(value);
  // Excel/Sheets formulas must never be executable when a tool returns text.
  if(typeof value!=='number' && (/^[\s\uFEFF]*[=+\-@]/.test(text)||/^[\t\r\n]/.test(text))) text="'"+text;
  return '"'+text.replaceAll('"','""')+'"';
}
const scopeLabel:Record<string,string>={organization:'组织',region:'区域',customer:'客户',status:'状态',date:'日期',date_from:'开始日期',date_to:'结束日期',time_range:'时间范围',timezone:'时区',factory:'工厂',aggregation:'统计口径'};
export function resultCSV(result: BusinessResult, runId: string, eventId: number) {
  const meta:unknown[][]=[['结果',result.title],['任务编号',runId],['结果事件',eventId],['数据性质',result.synthetic===true?'合成测试数据':result.synthetic===false?'工具标记非合成':'工具未说明'],['数据时间',result.data_time||'未提供'],['导出范围','仅当前已返回的结果，不代表未取回的明细'],['已返回行数',result.rows.length],['结果总行数',result.total_rows??'未提供'],['覆盖情况',result.truncated?'已截取':result.complete===true?'工具声明完整':result.complete===false?'部分结果':'完整性未说明'],...Object.entries(result.scope||{}).map(([k,v])=>[scopeLabel[k]||k,v]),...result.warnings.map(w=>['提示',w]),[],result.columns.map(c=>c.label+(c.unit?`（${c.unit}）`:'')),...result.rows.map(row=>result.columns.map(c=>row[c.key]))];
  return '\uFEFF'+meta.map(row=>row.map(csvCell).join(',')).join('\r\n');
}
const displayCell=(v:Cell)=>v===null?'—':typeof v==='boolean'?(v?'是':'否'):typeof v==='number'?(v!==0&&Math.abs(v)<0.000001?String(v):new Intl.NumberFormat('zh-CN',{maximumSignificantDigits:21}).format(v)):v;
export function BusinessResultCard({result,runId,eventId}:{result:BusinessResult;runId:string;eventId:number}) {
  const numeric=result.columns.filter(c=>!/^(id|.*_id|code|.*编号)$/i.test(c.key)&&result.rows.some(r=>typeof r[c.key]==='number')&&result.rows.every(r=>r[c.key]===null||(typeof r[c.key]==='number'&&Number(r[c.key])>=0)));
  const labels=result.columns.filter(c=>result.rows.some(r=>typeof r[c.key]==='string'));
  const [view,setView]=useState<'table'|'chart'>('table');
  const [metric,setMetric]=useState(numeric[0]?.key||'');const [label,setLabel]=useState(labels[0]?.key||'');
  const [source,setSource]=useState<string|null>(null);const [notice,setNotice]=useState('');
  const chartRows=result.rows.filter(r=>typeof r[metric]==='number').slice(0,20);
  const max=Math.max(0,...chartRows.map(r=>Number(r[metric])));
  const metricColumn=numeric.find(c=>c.key===metric);
  const canChart=numeric.length>0&&labels.length>0&&result.rows.length>1;
  const download=()=>{const blob=new Blob([resultCSV(result,runId,eventId)],{type:'text/csv;charset=utf-8;'});const href=URL.createObjectURL(blob);const a=document.createElement('a');a.href=href;a.download=`业务结果-${runId.replace(/[^A-Za-z0-9_-]/g,'').slice(0,50)}-${eventId}.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(href),1000);setNotice('已生成 CSV，包含查询范围与单位。CSV 不保存列类型；编号、日期等列请按文本导入。');};
  return <section className="business-result" aria-label={result.title}>
    <header className="business-result-heading"><div><span className="br-eyebrow"><Database size={14}/>工具返回的业务结果</span><h3>{result.title}</h3></div>{result.synthetic===true&&<span className="br-synthetic">合成测试数据</span>}</header>
    {result.summary&&<p className="br-summary">{result.summary}</p>}
    <div className="br-scope">{Object.entries(result.scope||{}).map(([key,value])=><span key={key}><small>{scopeLabel[key]||key}</small>{typeof value==='object'?JSON.stringify(value):String(value??'未提供')}</span>)}<span><small>数据时间</small>{result.data_time||'未提供'}</span></div>
    <div className="br-toolbar"><div className="br-tabs" aria-label="结果视图"><button type="button" aria-pressed={view==='table'} onClick={()=>setView('table')}><Table2 size={15}/>表格</button>{canChart&&<button type="button" aria-pressed={view==='chart'} onClick={()=>setView('chart')}><BarChart3 size={15}/>图表</button>}</div><button type="button" className="br-export" title="下载当前结果CSV，编号类字段请按文本导入" onClick={download}><Download size={15}/>导出当前结果 CSV</button></div>
    <p className="br-coverage">已返回 {result.rows.length} 行{result.total_rows!==null?` · 工具报告结果总行数 ${result.total_rows} 行`:' · 结果总行数未提供'} · {result.truncated?'结果已截取':result.complete===true?'工具声明完整':result.complete===false?'部分结果':'完整性未说明'}</p>
    {!result.rows.length?<p className="br-empty">本次查询未返回记录，请核对上方范围。</p>:view==='table'?<div className="br-table-scroll" tabIndex={0} aria-label="业务结果表格，可横向滚动"><table><thead><tr>{result.columns.map(c=><th key={c.key} scope="col">{c.label}{c.unit&&<small>（{c.unit}）</small>}</th>)}</tr></thead><tbody>{result.rows.map((row,i)=><tr key={i}>{result.columns.map(c=><td key={c.key} className={typeof row[c.key]==='number'?'br-number':''}>{displayCell(row[c.key])}</td>)}</tr>)}</tbody></table></div>:<div className="br-chart"><div className="br-chart-options"><label>分组<select aria-label="图表分组" value={label} onChange={e=>setLabel(e.target.value)}>{labels.map(c=><option value={c.key} key={c.key}>{c.label}</option>)}</select></label><label>指标<select aria-label="图表指标" value={metric} onChange={e=>setMetric(e.target.value)}>{numeric.map(c=><option value={c.key} key={c.key}>{c.label}{c.unit?`（${c.unit}）`:''}</option>)}</select></label></div><p className="br-chart-caption">{metricColumn?.label}{metricColumn?.unit?` · ${metricColumn.unit}`:''} · 零起点 · 当前结果前 {chartRows.length} 行，不重新聚合</p>{chartRows.map((row,i)=><div className="br-bar-row" key={i}><span title={String(row[label]??'未提供')}>{String(row[label]??'未提供')}</span><div className="br-bar-track"><div style={{width:`${max>0?Number(row[metric])/max*100:0}%`}}/></div><strong>{displayCell(row[metric])}</strong></div>)}</div>}
    {!!result.warnings.length&&<div className="br-warnings">{result.warnings.map((w,i)=><p key={i}>{w}</p>)}</div>}
    <footer className="br-footer"><span>结果编号 {eventId} · 本次回包版本</span><div>{result.source_ids?.map((id,i)=><button key={id} onClick={()=>setSource(id)}><Database size={13}/>依据 {i+1}</button>)}</div></footer>
    {notice&&<p className="br-coverage" role="status">{notice}</p>}{source&&<SourceDialog id={source} onClose={()=>setSource(null)}/>}
  </section>;
}
export default function BusinessResults({events,runId,status}:{events:StreamEvent[];runId:string;status:RunStatus}) {
  const results=useMemo(()=>resultsFromEvents(events,runId,status),[events,runId,status]);
  return <>{results.map(({event,result})=><BusinessResultCard key={`${runId}-${event.id}`} result={result} runId={runId} eventId={event.id}/>)}</>;
}
