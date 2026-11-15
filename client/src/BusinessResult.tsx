import { useMemo, useState } from 'react';
import { BarChart3, ChevronDown, Database, Download, LayoutList, Table2 } from 'lucide-react';
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
  const meta:unknown[][]=[['结果',result.title],['任务编号',runId],['结果事件',eventId],['数据性质',result.synthetic===true?'合成测试数据':result.synthetic===false?'工具标记非合成':'工具未说明'],['数据时间',result.data_time||'未提供'],['导出范围','仅当前已返回的结果，不代表未取回的明细'],['已返回行数',result.rows.length],['结果总行数',result.total_rows??'未提供'],['覆盖情况',result.truncated?'已截取':result.complete===true?'工具声明完整':result.complete===false?'部分结果':'完整性未说明'],...Object.entries(result.scope||{}).map(([k,v])=>[scopeLabel[k]||k,v]),...result.warnings.map(w=>['提示',w]),...(result.source_ids||[]).map(id=>['来源 ID',id]),[],result.columns.map(c=>c.label+(c.unit?`（${c.unit}）`:'')),...result.rows.map(row=>result.columns.map(c=>row[c.key]))];
  return '\uFEFF'+meta.map(row=>row.map(csvCell).join(',')).join('\r\n');
}
const displayCell=(v:Cell)=>v===null?'—':typeof v==='boolean'?(v?'是':'否'):typeof v==='number'?(v!==0&&Math.abs(v)<0.000001?String(v):new Intl.NumberFormat('zh-CN',{maximumSignificantDigits:21}).format(v)):v;
/** Preview returned fields only; never calculate a total from a partial result. */
export function objectPreview(result: BusinessResult, row: Record<string, Cell>, index: number) {
  const identity = result.columns.find(column => typeof row[column.key] === 'string' && row[column.key] !== '')
    || result.columns.find(column => /^(id|.*_id|code|.*编号)$/i.test(column.key) && row[column.key] !== null);
  return { title: identity ? `${identity.label}：${displayCell(row[identity.key])}` : `结果 ${index + 1}`,
    fields: result.columns.filter(column => column.key !== identity?.key) };
}
export function BusinessResultCard({result,runId,eventId}:{result:BusinessResult;runId:string;eventId:number}) {
  const numeric=result.columns.filter(c=>!/^(id|.*_id|code|.*编号)$/i.test(c.key)&&result.rows.some(r=>typeof r[c.key]==='number')&&result.rows.every(r=>r[c.key]===null||(typeof r[c.key]==='number'&&Number(r[c.key])>=0)));
  const labels=result.columns.filter(c=>result.rows.some(r=>typeof r[c.key]==='string'));
  const [view,setView]=useState<'objects'|'table'|'chart'>('objects');
  const [visibleObjects,setVisibleObjects]=useState(6);const [visibleTableRows,setVisibleTableRows]=useState(20);
  const [metric,setMetric]=useState(numeric[0]?.key||'');const [label,setLabel]=useState(labels[0]?.key||'');
  const [source,setSource]=useState<string|null>(null);const [notice,setNotice]=useState('');
  const chartRows=result.rows.filter(r=>typeof r[metric]==='number').slice(0,20);
  const max=Math.max(0,...chartRows.map(r=>Number(r[metric])));
  const metricColumn=numeric.find(c=>c.key===metric);
  const canChart=numeric.length>0&&labels.length>0&&result.rows.length>1;
  const scope=Object.entries(result.scope||{});
  const timeScope=scope.filter(([key])=>['date','date_from','date_to','time_range'].includes(key));
  const objectScope=['organization','region','customer','status','factory'].flatMap(key=>scope.filter(([field])=>field===key)).slice(0,2);
  const primaryScope=[...objectScope,...timeScope];
  const scopeText=(value:unknown)=>value===null||value===undefined?'未提供':typeof value==='object'?JSON.stringify(value):String(value);
  const field=(column:ResultColumn,row:Record<string,Cell>)=><div key={column.key}><dt>{column.label}{column.unit&&<small> · {column.unit}</small>}</dt><dd className={typeof row[column.key]==='number'?'br-number':''}>{displayCell(row[column.key])}</dd></div>;
  const download=()=>{const blob=new Blob([resultCSV(result,runId,eventId)],{type:'text/csv;charset=utf-8;'});const href=URL.createObjectURL(blob);const a=document.createElement('a');a.href=href;a.download=`业务结果-${runId.replace(/[^A-Za-z0-9_-]/g,'').slice(0,50)}-${eventId}.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(href),1000);setNotice('已生成 CSV，包含查询范围与单位。CSV 不保存列类型；编号、日期等列请按文本导入。');};
  return <section className="business-result br-object-result" aria-label={result.title}>
    <header className="business-result-heading"><div><span className="br-eyebrow"><Database size={14}/>业务查询结果</span><h3>{result.title}</h3></div>{result.synthetic===true&&<span className="br-synthetic">合成测试数据</span>}</header>
    {!!primaryScope.length&&<div className="br-condition-tags" aria-label="主要查询条件">{primaryScope.map(([key,value])=><span key={key}><small>{scopeLabel[key]||key}</small>{scopeText(value)}</span>)}</div>}
    {result.summary&&<p className="br-summary">{result.summary}</p>}
    <p className="br-coverage">已返回 {result.rows.length} 行{result.total_rows!==null?` · 工具报告结果总行数 ${result.total_rows} 行`:' · 结果总行数未提供'} · {result.truncated?'结果已截取':result.complete===true?'工具声明完整':result.complete===false?'部分结果':'完整性未说明'}</p>
    {!!result.warnings.length&&<div className="br-warnings">{result.warnings.map((w,i)=><p key={i}>{w}</p>)}</div>}
    <div className="br-toolbar"><div className="br-tabs" aria-label="结果视图"><button type="button" aria-pressed={view==='objects'} onClick={()=>setView('objects')}><LayoutList size={15}/>摘要</button><button type="button" aria-pressed={view==='table'} onClick={()=>setView('table')}><Table2 size={15}/>表格</button>{canChart&&<button type="button" aria-pressed={view==='chart'} onClick={()=>setView('chart')}><BarChart3 size={15}/>图表</button>}</div><button type="button" className="br-export" title="下载当前结果CSV，编号类字段请按文本导入" onClick={download}><Download size={15}/>导出当前结果 CSV</button></div>
    {!result.rows.length?<p className="br-empty">本次查询未返回记录。请核对下方查询范围与依据，调整条件后重新查询。</p>:view==='objects'?<>
      <div className={`br-objects${result.rows.length===1?' br-single-object':''}`}>{result.rows.slice(0,visibleObjects).map((row,index)=>{
        const preview=objectPreview(result,row,index);
        return <article className="br-object" key={index}><h4>{preview.title}</h4><dl className="br-object-fields">{preview.fields.slice(0,3).map(column=>field(column,row))}</dl>{preview.fields.length>3&&<details className="br-field-details"><summary>查看全部 {result.columns.length} 个字段<ChevronDown size={14}/></summary><dl className="br-object-fields">{result.columns.map(column=>field(column,row))}</dl></details>}</article>;
      })}</div>
      {result.rows.length>visibleObjects&&<div className="br-more"><span>已显示 {visibleObjects} / {result.rows.length} 行已返回结果</span><button type="button" onClick={()=>setVisibleObjects(count=>count+6)}>再显示 {Math.min(6,result.rows.length-visibleObjects)} 行<ChevronDown size={14}/></button></div>}
    </>:view==='table'?<><div className="br-table-scroll" tabIndex={0} aria-label="业务结果表格，可横向滚动"><table><thead><tr>{result.columns.map(c=><th key={c.key} scope="col">{c.label}{c.unit&&<small>（{c.unit}）</small>}</th>)}</tr></thead><tbody>{result.rows.slice(0,visibleTableRows).map((row,i)=><tr key={i}>{result.columns.map(c=><td key={c.key} className={typeof row[c.key]==='number'?'br-number':''}>{displayCell(row[c.key])}</td>)}</tr>)}</tbody></table></div>{result.rows.length>visibleTableRows&&<div className="br-more"><span>已显示 {visibleTableRows} / {result.rows.length} 行已返回结果</span><button type="button" onClick={()=>setVisibleTableRows(count=>count+20)}>再显示 {Math.min(20,result.rows.length-visibleTableRows)} 行<ChevronDown size={14}/></button></div>}</>:<div className="br-chart"><div className="br-chart-options"><label>分组<select aria-label="图表分组" value={label} onChange={e=>setLabel(e.target.value)}>{labels.map(c=><option value={c.key} key={c.key}>{c.label}</option>)}</select></label><label>指标<select aria-label="图表指标" value={metric} onChange={e=>setMetric(e.target.value)}>{numeric.map(c=><option value={c.key} key={c.key}>{c.label}{c.unit?`（${c.unit}）`:''}</option>)}</select></label></div><p className="br-chart-caption">{metricColumn?.label}{metricColumn?.unit?` · ${metricColumn.unit}`:''} · 零起点 · 当前结果前 {chartRows.length} 行，不重新聚合</p>{chartRows.map((row,i)=><div className="br-bar-row" key={i}><span title={String(row[label]??'未提供')}>{String(row[label]??'未提供')}</span><div className="br-bar-track"><div style={{width:`${max>0?Number(row[metric])/max*100:0}%`}}/></div><strong>{displayCell(row[metric])}</strong></div>)}</div>}
    <details className="br-evidence" open={!result.rows.length || undefined}><summary><span><Database size={15}/>查询范围与依据{result.source_ids?.length?` · ${result.source_ids.length}`:''}</span><ChevronDown size={15}/></summary><div className="br-evidence-body"><dl className="br-evidence-scope">{scope.map(([key,value])=><div key={key}><dt>{scopeLabel[key]||key}</dt><dd>{scopeText(value)}</dd></div>)}<div><dt>数据时间</dt><dd>{result.data_time||'未提供'}</dd></div></dl><div className="br-source-actions">{result.source_ids?.length?result.source_ids.map((id,i)=><button type="button" key={id} onClick={()=>setSource(id)}><Database size={14}/>查看依据 {i+1}</button>):<span>本次结果未提供来源详情。</span>}</div><p>仅展示本次返回的数据。摘要、表格、图表和导出使用同一份结果。</p></div></details>
    <footer className="br-footer"><span>结果编号 {eventId} · 当前结果版本</span><span>数据时间：{result.data_time||'未提供'}</span></footer>
    {notice&&<p className="br-coverage" role="status">{notice}</p>}{source&&<SourceDialog id={source} onClose={()=>setSource(null)}/>}
  </section>;
}

export default function BusinessResults({events,runId,status}:{events:StreamEvent[];runId:string;status:RunStatus}) {
  const results=useMemo(()=>resultsFromEvents(events,runId,status),[events,runId,status]);
  return <>{results.map(({event,result})=><BusinessResultCard key={`${runId}-${event.id}`} result={result} runId={runId} eventId={event.id}/>)}</>;
}
