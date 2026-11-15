import { useEffect, useId, useState } from 'react';
import { ArrowDown, ArrowUp, ChevronDown, ChevronLeft, ChevronRight, Database, Download, Filter, Network, RefreshCw, Search } from 'lucide-react';
import { api, errorText } from './api';
import { Button, EmptyState, ErrorNotice, Loader } from './components';
import './business-data.css';

export interface DataColumn { key:string; label:string; type?:string; format?:'currency_cents'; searchable?:boolean }
export interface DataTable { id:string; name:string; description:string; scope:'demo'|'public'|'business'; connection_id:string; columns:DataColumn[] }
export interface DataPage { table:DataTable; total_rows:number; matched_rows:number; offset:number; limit:number; rows:Record<string,unknown>[]; duration_ms:number }
const MAX_OFFSET=1_000_000;
export const dataCell=(value:unknown,column:DataColumn)=> value===null||value===undefined?'—':column.format==='currency_cents'&&typeof value==='number'?(value/100).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2}):String(value);
const provenance={demo:'演示数据',public:'公开数据',business:'业务数据'};
export const dataPageCsv=(value:DataPage)=> {
  const safe=(s:string)=>'"'+(/^[\s\u0000-\u001f]*[=+@-]/.test(s)?"'"+s:s).replaceAll('"','""')+'"';
  return '\ufeff'+[value.table.columns.map(c=>safe(c.label)).join(','),...value.rows.map(row=>value.table.columns.map(c=>safe(dataCell(row[c.key],c))).join(','))].join('\r\n');
};
export default function BusinessDataPage({onConnections}:{onConnections:()=>void}) {
  const [filtersExpanded,setFiltersExpanded]=useState(false);
  const filtersId=useId();
  const [tables,setTables]=useState<DataTable[]>([]),[selected,setSelected]=useState(''),[catalogLoading,setCatalogLoading]=useState(true),[catalogError,setCatalogError]=useState('');
  const [q,setQ]=useState(''),[field,setField]=useState(''),[value,setValue]=useState('');
  const [query,setQuery]=useState({q:'',filter_column:'',filter_value:'',offset:0,sort:'',direction:'asc'});
  const [data,setData]=useState<DataPage|null>(null),[loading,setLoading]=useState(false),[error,setError]=useState(''),[epoch,setEpoch]=useState(0);
  const [catalogEpoch,setCatalogEpoch]=useState(0);
  useEffect(()=>{const c=new AbortController();setCatalogLoading(true);setCatalogError('');api<DataTable[]>('/business-data',{signal:c.signal}).then(items=>{if(c.signal.aborted)return;setTables(items);setSelected(previous=>items.some(t=>t.id===previous)?previous:items[0]?.id||'');}).catch(e=>{if(!c.signal.aborted)setCatalogError(errorText(e));}).finally(()=>{if(!c.signal.aborted)setCatalogLoading(false);});return()=>c.abort();},[catalogEpoch]);
  useEffect(()=>{if(!selected)return;const c=new AbortController();setLoading(true);setError('');const params=new URLSearchParams({...query,offset:String(query.offset),limit:'50'});api<DataPage>(`/business-data/${encodeURIComponent(selected)}/rows?${params}`,{signal:c.signal}).then(result=>{if(!c.signal.aborted)setData(result);}).catch(e=>{if(!c.signal.aborted){setData(null);setError(errorText(e));}}).finally(()=>{if(!c.signal.aborted)setLoading(false);});return()=>c.abort();},[selected,query,epoch]);
  const table=tables.find(t=>t.id===selected);
  const choose=(id:string)=>{setData(null);setSelected(id);setQ('');setField('');setValue('');setQuery({q:'',filter_column:'',filter_value:'',offset:0,sort:'',direction:'asc'});};
  const exportPage=()=>{if(!data||loading||data.table.id!==selected)return;const url=URL.createObjectURL(new Blob([dataPageCsv(data)],{type:'text/csv;charset=utf-8;'}));const a=document.createElement('a');a.href=url;a.download=`${data.table.name}-第${Math.floor(data.offset/data.limit)+1}页.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
  return <section className="data-page">
    <header className="page-heading"><div><p className="eyebrow">数据与资料</p><h1>业务数据</h1><p>查看业务表、筛选记录，核对助手给出的结果。</p></div><Button variant="secondary" onClick={onConnections}><Network size={16}/>管理数据连接</Button></header>
    <ErrorNotice message={catalogError}/>
    {catalogError&&<Button variant="secondary" onClick={()=>setCatalogEpoch(e=>e+1)}>重新读取数据目录</Button>}
    {catalogLoading?<Loader text="正在读取数据目录…"/>:!tables.length&&!catalogError?<EmptyState icon={<Database/>} title="还没有可浏览的业务表" description="连接 MCP 后，由管理员登记允许查看的数据表。工具能回答问题，不一定提供明细浏览接口。"/>:null}
    {table&&<>
      <nav className="data-table-tabs" aria-label="业务数据表">{tables.map(t=><button key={t.id} aria-current={selected===t.id?'page':undefined} onClick={()=>choose(t.id)}><Database size={16}/>{t.name}</button>)}</nav>
      <div className="data-sheet">
        <div className="data-sheet-heading"><div><h2>{table.name}<span className="data-provenance">{provenance[table.scope]}</span><span className="data-provenance">只读</span></h2><p>{table.description}</p></div><Button variant="secondary" busy={loading} onClick={()=>setEpoch(e=>e+1)}><RefreshCw size={15}/>刷新</Button></div>
        <button type="button" className="data-filter-toggle" aria-expanded={filtersExpanded} aria-controls={filtersId} onClick={()=>setFiltersExpanded(open=>!open)}><span><Filter size={16}/>筛选记录</span><span>{query.q.trim()||query.filter_column&&query.filter_value?'已应用筛选':'全部记录'}<ChevronDown size={16}/></span></button>
        <form id={filtersId} className={`data-filters${filtersExpanded?' is-expanded':''}`} onSubmit={e=>{e.preventDefault();setQuery(old=>({...old,q,filter_column:field,filter_value:value,offset:0}));setFiltersExpanded(false);}}>
          <label className="data-search"><span>搜索记录</span><div><Search size={16}/><input value={q} maxLength={100} onChange={e=>setQ(e.target.value)} placeholder="客户、区域或记录编号"/></div></label>
          <label><span>筛选字段</span><select value={field} onChange={e=>{setField(e.target.value);setValue('');}}><option value="">不限</option>{table.columns.filter(c=>c.format!=='currency_cents').map(c=><option key={c.key} value={c.key}>{c.label}</option>)}</select></label>
          <label><span>等于</span><input value={value} onChange={e=>setValue(e.target.value)} disabled={!field} placeholder={field?'输入完整值':'先选字段'} maxLength={100}/></label>
          <Button busy={loading} type="submit"><Filter size={15}/>查询</Button>
          <button type="button" className="data-reset" onClick={()=>{setQ('');setField('');setValue('');setQuery(old=>({...old,q:'',filter_column:'',filter_value:'',offset:0}));}}>清除筛选</button>
        </form>
        <ErrorNotice message={error}/>
        {error&&<Button variant="secondary" onClick={()=>setEpoch(e=>e+1)}>重新读取记录</Button>}
        <div className="data-sheet-status" role="status"><span>{loading?'正在查询…':data?`共 ${data.total_rows.toLocaleString()} 条 · 当前匹配 ${data.matched_rows.toLocaleString()} 条`:'等待查询'}</span><Button variant="secondary" disabled={!data||loading||!data.rows.length} onClick={exportPage}><Download size={15}/>导出当前页</Button></div>
        <div className="data-grid-wrap" aria-busy={loading}>
          <table className="data-grid"><thead><tr>{table.columns.map(c=><th key={c.key} aria-sort={query.sort===c.key?(query.direction==='asc'?'ascending':'descending'):'none'}><button disabled={loading} onClick={()=>setQuery(old=>({...old,offset:0,sort:c.key,direction:old.sort===c.key&&old.direction==='asc'?'desc':'asc'}))}>{c.label}{query.sort===c.key?(query.direction==='asc'?<ArrowUp size={13}/>:<ArrowDown size={13}/>):null}</button></th>)}</tr></thead><tbody>{(data?.table.id===selected?data.rows:[]).map((row,i)=><tr key={(data?.offset||0)+i}>{table.columns.map(c=><td key={c.key} className={typeof row[c.key]==='number'?'numeric':''}>{dataCell(row[c.key],c)}</td>)}</tr>)}</tbody></table>
          {!loading&&data&&!data.rows.length&&<div className="data-empty">没有匹配的记录，试试调整筛选条件。</div>}
        </div>
        {data&&data.table.id===selected&&<footer className="data-pagination"><span>{data.matched_rows?`${data.offset+1}–${data.offset+data.rows.length} / ${data.matched_rows.toLocaleString()}`:'0 条记录'}<small>每页 {data.limit} 条 · 直接读取数据表，不消耗模型 Token</small></span><div><Button variant="secondary" disabled={loading||data.offset===0} onClick={()=>setQuery(old=>({...old,offset:0}))}>首页</Button><Button variant="secondary" disabled={loading||data.offset===0} onClick={()=>setQuery(old=>({...old,offset:Math.max(0,data.offset-data.limit)}))}><ChevronLeft size={16}/>上一页</Button><Button variant="secondary" disabled={loading||data.offset+data.limit>=data.matched_rows||data.offset>=MAX_OFFSET} onClick={()=>setQuery(old=>({...old,offset:Math.min(MAX_OFFSET,data.offset+data.limit)}))}>下一页<ChevronRight size={16}/></Button><Button variant="secondary" disabled={loading||data.offset+data.limit>=data.matched_rows||data.offset>=MAX_OFFSET} onClick={()=>setQuery(old=>({...old,offset:Math.min(MAX_OFFSET,Math.max(0,Math.ceil(data.matched_rows/data.limit)-1)*data.limit)}))}>末页</Button></div></footer>}
        {data&&data.matched_rows>MAX_OFFSET+data.limit&&<p className="capability-note">当前最多浏览前 {(MAX_OFFSET+data.limit).toLocaleString()} 条匹配记录。请缩小筛选范围后查看其余记录。</p>}
        <details className="data-origin"><summary>数据来源与使用范围</summary><p>{table.scope==='demo'?'用于体验查询、分析和核对流程，不代表客户生产实绩。':table.scope==='public'?'来自公开资料，适用范围以原始发布说明为准。':'由管理员登记的数据表，访问范围随对应连接权限控制。'} 当前仅展示管理员开放的表与字段；MCP 接通不代表该系统所有数据都会自动出现。</p></details>
      </div>
    </>}
  </section>;
}
