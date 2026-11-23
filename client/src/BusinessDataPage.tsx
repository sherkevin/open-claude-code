import { useEffect, useId, useRef, useState } from 'react';
import { ArrowDown, ArrowUp, ChevronDown, ChevronLeft, ChevronRight, Database, Download, Filter, Network, RefreshCw, Search, Plus, X } from 'lucide-react';
import { api, errorText } from './api';
import { Button, EmptyState, ErrorNotice, Loader } from './components';
import { BusinessIcon, businessIconFor } from './BusinessIcon';
import './business-data.css';

export interface DataColumn { key:string; label:string; type?:string; format?:'currency_cents'; searchable?:boolean }
export interface DataTable { id:string; name:string; description:string; scope:'demo'|'public'|'business'; connection_id:string; columns:DataColumn[] }
export interface DataPage { table:DataTable; total_rows:number; matched_rows:number; offset:number; limit:number; rows:Record<string,unknown>[]; duration_ms:number }
const MAX_OFFSET=1_000_000;
export const dataCell=(value:unknown,column:DataColumn)=> value===null||value===undefined?'—':column.format==='currency_cents'&&typeof value==='number'?(value/100).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2}):String(value);
const provenance={demo:'非生产数据',public:'公开数据',business:'业务数据'};
export const dataPageCsv=(value:DataPage)=> {
  const safe=(s:string)=>'"'+(/^[\s\u0000-\u001f]*[=+@-]/.test(s)?"'"+s:s).replaceAll('"','""')+'"';
  return '\ufeff'+[value.table.columns.map(c=>safe(c.label)).join(','),...value.rows.map(row=>value.table.columns.map(c=>safe(dataCell(row[c.key],c))).join(','))].join('\r\n');
};
export type DataCondition = { field:string; operator:string; value?:string; value_to?:string };
type FilterRow = DataCondition & { id:number };
type FilterLogic = 'and'|'or';
export const MAX_DATA_CONDITIONS = 12;
const EMPTY_OPS = new Set(['is_empty','is_not_empty']);
const OP_LABELS:Record<string,string> = {eq:'等于',ne:'不等于',contains:'包含',not_contains:'不包含',starts_with:'开头是',gt:'大于',gte:'大于或等于',lt:'小于',lte:'小于或等于',between:'介于',is_empty:'为空',is_not_empty:'不为空'};
export const dataColumnType=(column:DataColumn)=>column.format==='currency_cents'?'number':['text','integer','number','date','datetime','boolean'].includes(column.type||'')?column.type!:'text';
export const dataOperators=(column:DataColumn)=>{
  const kind=dataColumnType(column), ordered=['integer','number','date','datetime'].includes(kind);
  const operators=['eq','ne',...(kind==='text'?['contains','not_contains','starts_with']:ordered?['gt','gte','lt','lte','between']:[]),'is_empty','is_not_empty'];
  return operators.map(value=>({value,label:kind==='date'||kind==='datetime'?({gt:'晚于',gte:'不早于',lt:'早于',lte:'不晚于',between:'日期范围'} as Record<string,string>)[value]||OP_LABELS[value]:OP_LABELS[value]}));
};
export function prepareDataConditions(rows:DataCondition[],columns:DataColumn[]):DataCondition[] {
  if(rows.length>MAX_DATA_CONDITIONS)throw new Error(`最多添加 ${MAX_DATA_CONDITIONS} 条条件。`);
  return rows.map((row,index)=>{
    const column=columns.find(c=>c.key===row.field), prefix=`第 ${index+1} 条条件`;
    if(!column)throw new Error(`${prefix}请选择字段。`);
    if(!dataOperators(column).some(op=>op.value===row.operator))throw new Error(`${prefix}请选择适用的条件。`);
    if(EMPTY_OPS.has(row.operator))return {field:row.field,operator:row.operator};
    const kind=dataColumnType(column);
    const validate=(raw:string|undefined)=>{
      const value=(raw||'').trim();
      if(!value)throw new Error(`${prefix}请填写${row.operator==='between'?'完整范围':'值'}；查空值请选择“为空”。`);
      if(value.length>100)throw new Error(`${prefix}的值不能超过 100 字。`);
      if(['number','integer'].includes(kind)){
        if(!/^-?\d+(?:\.\d+)?$/.test(value))throw new Error(`${prefix}请填写数字，不含千位分隔符。`);
        if(kind==='integer'&&!Number.isInteger(Number(value)))throw new Error(`${prefix}请填写整数。`);
        if(column.format==='currency_cents'&&(value.split('.')[1]?.length||0)>2)throw new Error(`${prefix}金额按元填写，最多两位小数。`);
      }
      if(kind==='date'&&(!/^\d{4}-\d{2}-\d{2}$/.test(value)||!Number.isFinite(Date.parse(value))||new Date(value).toISOString().slice(0,10)!==value))throw new Error(`${prefix}请填写有效日期。`);
      if(kind==='datetime'&&!Number.isFinite(Date.parse(value)))throw new Error(`${prefix}请填写有效日期时间。`);
      if(kind==='boolean'&&!['true','false'].includes(value))throw new Error(`${prefix}请选择是或否。`);
      return value;
    };
    const value=validate(row.value), valueTo=row.operator==='between'?validate(row.value_to):undefined;
    if(valueTo!==undefined&&(['number','integer'].includes(kind)?Number(value)>Number(valueTo):value>valueTo))throw new Error(`${prefix}的范围起点不能大于终点。`);
    // Money stays as a decimal yuan string; only the server converts to stored cents.
    return {field:row.field,operator:row.operator,value,...(valueTo!==undefined?{value_to:valueTo}:{})};
  });
}
const initialQuery=()=>({q:'',filters:[] as DataCondition[],filter_logic:'and' as FilterLogic,offset:0,sort:'',direction:'asc'});
export default function BusinessDataPage({onConnections}:{onConnections:()=>void}) {
  const [filtersExpanded,setFiltersExpanded]=useState(false);
  const filtersId=useId();
  const [tables,setTables]=useState<DataTable[]>([]),[selected,setSelected]=useState(''),[catalogLoading,setCatalogLoading]=useState(true),[catalogError,setCatalogError]=useState('');
  const [q,setQ]=useState(''),[conditions,setConditions]=useState<FilterRow[]>([]),[logic,setLogic]=useState<FilterLogic>('and'),[filterError,setFilterError]=useState('');
  const conditionSequence=useRef(0),conditionPanel=useRef<HTMLDivElement>(null);
  const [query,setQuery]=useState(initialQuery);
  const [data,setData]=useState<DataPage|null>(null),[loading,setLoading]=useState(false),[error,setError]=useState(''),[epoch,setEpoch]=useState(0);
  const [catalogEpoch,setCatalogEpoch]=useState(0);
  useEffect(()=>{const c=new AbortController();setCatalogLoading(true);setCatalogError('');api<DataTable[]>('/business-data',{signal:c.signal}).then(items=>{if(c.signal.aborted)return;setTables(items);setSelected(previous=>items.some(t=>t.id===previous)?previous:items[0]?.id||'');}).catch(e=>{if(!c.signal.aborted)setCatalogError(errorText(e));}).finally(()=>{if(!c.signal.aborted)setCatalogLoading(false);});return()=>c.abort();},[catalogEpoch]);
  useEffect(()=>{if(!selected)return;const c=new AbortController();setLoading(true);setError('');const params=new URLSearchParams({...query,filters:JSON.stringify(query.filters),offset:String(query.offset),limit:'50'});api<DataPage>(`/business-data/${encodeURIComponent(selected)}/rows?${params}`,{signal:c.signal}).then(result=>{if(!c.signal.aborted)setData(result);}).catch(e=>{if(!c.signal.aborted){setData(null);setError(errorText(e));}}).finally(()=>{if(!c.signal.aborted)setLoading(false);});return()=>c.abort();},[selected,query,epoch]);
  const table=tables.find(t=>t.id===selected);
  const clearFilters=()=>{setQ('');setConditions([]);setLogic('and');setFilterError('');setFiltersExpanded(false);setQuery(old=>({...initialQuery(),sort:old.sort,direction:old.direction}));};
  const choose=(id:string)=>{setData(null);setSelected(id);clearFilters();setQuery(initialQuery());};
  const changeCondition=(id:number,patch:Partial<DataCondition>)=>{setFilterError('');setConditions(old=>old.map(row=>row.id===id?{...row,...patch}:row));};
  const addCondition=()=>{if(!table||conditions.length>=MAX_DATA_CONDITIONS)return;setConditions(old=>[...old,{id:++conditionSequence.current,field:table.columns[0].key,operator:'eq',value:''}]);setFiltersExpanded(true);setFilterError('');requestAnimationFrame(()=>conditionPanel.current?.querySelector<HTMLSelectElement>('.data-condition:last-child select')?.focus());};
  const applyFilters=()=>{if(!table)return;try{const filters=prepareDataConditions(conditions,table.columns);setQuery(old=>({...old,q:q.trim(),filters,filter_logic:logic,offset:0}));setFilterError('');if(matchMedia('(max-width:800px)').matches)setFiltersExpanded(false);}catch(e){setFilterError(errorText(e));setFiltersExpanded(true);}};
  const appliedCount=query.filters.length;
  let conditionDirty=true;
  try { conditionDirty=JSON.stringify(prepareDataConditions(conditions,table?.columns||[]))!==JSON.stringify(query.filters); } catch { /* An unfinished condition is an unapplied edit. */ }
  const dirty=q.trim()!==query.q||logic!==query.filter_logic||conditionDirty;
  const exportPage=()=>{if(!data||loading||data.table.id!==selected)return;const url=URL.createObjectURL(new Blob([dataPageCsv(data)],{type:'text/csv;charset=utf-8;'}));const a=document.createElement('a');a.href=url;a.download=`${data.table.name}-第${Math.floor(data.offset/data.limit)+1}页.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
  return <section className="data-page">
    <header className="page-heading"><div><p className="eyebrow">数据与资料</p><h1>业务数据</h1><p>查看业务表、筛选记录，核对助手给出的结果。</p></div><Button variant="secondary" onClick={onConnections}><Network size={16}/>管理数据连接</Button></header>
    <ErrorNotice message={catalogError}/>
    {catalogError&&<Button variant="secondary" onClick={()=>setCatalogEpoch(e=>e+1)}>重新读取数据目录</Button>}
    {catalogLoading?<Loader text="正在读取数据目录…"/>:!tables.length&&!catalogError?<EmptyState icon={<Database/>} title="还没有可浏览的业务表" description="连接 MCP 后，由管理员登记允许查看的数据表。工具能回答问题，不一定提供明细浏览接口。"/>:null}
    {table&&<>
      <nav className="data-table-tabs" aria-label="业务数据表">{tables.map(t=><button key={t.id} aria-current={selected===t.id?'page':undefined} onClick={()=>choose(t.id)}><BusinessIcon icon={businessIconFor(t.name, t.id) ?? 'data-connect'} size={16} mono={selected !== t.id}/>{t.name}</button>)}</nav>
      <div className="data-sheet">
        <div className="data-sheet-heading"><div><h2>{table.name}<span className="data-provenance">{provenance[table.scope]}</span><span className="data-provenance">只读</span></h2><p>{table.description}</p></div><Button variant="secondary" busy={loading} onClick={()=>setEpoch(e=>e+1)}><RefreshCw size={15}/>刷新</Button></div>
        <form className="data-filters" noValidate onSubmit={e=>{e.preventDefault();applyFilters();}}>
          <div className="data-filter-toolbar">
            <label className="data-search"><span className="data-sr-only">搜索记录</span><div><Search size={16}/><input aria-label="搜索记录" value={q} maxLength={100} onChange={e=>{setQ(e.target.value);setFilterError('');}} placeholder="搜索客户、区域或记录编号"/></div></label>
            <button type="button" className="data-add-condition" disabled={conditions.length>=MAX_DATA_CONDITIONS} onClick={addCondition}><Plus size={15}/>添加条件</button>
            {conditions.length>0&&<button type="button" className="data-filter-toggle" aria-expanded={filtersExpanded} aria-controls={filtersId} onClick={()=>setFiltersExpanded(open=>!open)}><Filter size={15}/>{conditions.length} 条条件<ChevronDown size={14}/></button>}
            <Button busy={loading} type="submit">查询</Button>
            {(q||conditions.length>0||query.q||appliedCount>0)&&<button type="button" className="data-reset" aria-label="清除筛选" title="清除筛选" onClick={clearFilters}><X size={14}/><span>清除</span></button>}
          </div>
          {conditions.length>0&&<div ref={conditionPanel} id={filtersId} className="data-conditions" hidden={!filtersExpanded}>
            <div className="data-conditions-heading"><label><span>条件关系</span><select aria-label="条件关系" value={logic} onChange={e=>{setLogic(e.target.value as FilterLogic);setFilterError('');}}><option value="and">全部满足（AND）</option><option value="or">任一满足（OR）</option></select></label><span>{q.trim()?'关键词搜索仍须同时满足':'范围包含起点和终点'}</span></div>
            <div className="data-condition-list">{conditions.map((row,index)=>{
              const column=table.columns.find(c=>c.key===row.field)||table.columns[0],kind=dataColumnType(column),range=row.operator==='between',empty=EMPTY_OPS.has(row.operator);
              const inputProps={type:kind==='date'?'date':kind==='datetime'?'datetime-local':'text',inputMode:kind==='integer'?'numeric' as const:kind==='number'?'decimal' as const:undefined,maxLength:100};
              return <div key={row.id} className="data-condition">
                <span className="data-condition-number" aria-hidden="true">{index+1}</span>
                <label className="data-condition-field"><span>字段</span><select aria-label={`条件 ${index+1} 字段`} title={column.label} value={row.field} onChange={e=>changeCondition(row.id,{field:e.target.value,operator:'eq',value:'',value_to:undefined})}>{table.columns.map(c=><option key={c.key} value={c.key}>{c.label}</option>)}</select></label>
                <label className="data-condition-operator"><span>条件</span><select aria-label={`条件 ${index+1} 操作`} value={row.operator} onChange={e=>changeCondition(row.id,{operator:e.target.value,...(EMPTY_OPS.has(e.target.value)?{value:undefined,value_to:undefined}:{})})}>{dataOperators(column).map(op=><option key={op.value} value={op.value}>{op.label}</option>)}</select></label>
                <div className={`data-condition-values${range?' is-range':''}`}>
                  {empty?<span className="data-condition-empty">无需填写值</span>:<>
                    <label><span>{range?(kind==='datetime'?'起点（UTC）':'起点'):column.format==='currency_cents'?'金额（元）':kind==='datetime'?'时间（UTC）':'值'}</span>{kind==='boolean'?<select aria-label={`条件 ${index+1} 值`} value={row.value||''} onChange={e=>changeCondition(row.id,{value:e.target.value})}><option value="">请选择</option><option value="true">是</option><option value="false">否</option></select>:<input {...inputProps} aria-label={`条件 ${index+1} ${range?'起点':'值'}`} value={row.value||''} onChange={e=>changeCondition(row.id,{value:e.target.value})} placeholder={column.format==='currency_cents'?'按元填写，如 1200.50':kind==='integer'?'输入整数':'输入值'}/>}</label>
                    {range&&<><span className="data-range-divider">至</span><label><span>{kind==='datetime'?'终点（UTC）':'终点'}</span><input {...inputProps} aria-label={`条件 ${index+1} 终点`} value={row.value_to||''} onChange={e=>changeCondition(row.id,{value_to:e.target.value})} placeholder={column.format==='currency_cents'?'按元填写':'输入值'}/></label></>}
                  </>}
                </div>
                <button type="button" className="data-remove-condition" aria-label={`删除条件 ${index+1}`} title="删除条件" onClick={()=>{setConditions(old=>old.filter(c=>c.id!==row.id));setFilterError('');}}><X size={16}/></button>
              </div>;
            })}</div>
          </div>}
          <ErrorNotice message={filterError}/>
          {(appliedCount>0||query.q||dirty)&&<div className="data-applied-filters" role="status"><span>{query.q?`关键词“${query.q}” · `:''}{appliedCount?`已应用 ${appliedCount} 条条件 · ${query.filter_logic==='and'?'全部满足':'任一满足'}`:'全部记录'}</span>{dirty&&<span>有未应用的修改</span>}</div>}
        </form>
        <ErrorNotice message={error}/>
        {error&&<Button variant="secondary" onClick={()=>setEpoch(e=>e+1)}>重新读取记录</Button>}
        <div className="data-sheet-status" role="status"><span>{loading?'正在查询…':data?`共 ${data.total_rows.toLocaleString()} 条 · 当前匹配 ${data.matched_rows.toLocaleString()} 条`:'等待查询'}</span><Button variant="secondary" disabled={!data||loading||!data.rows.length} onClick={exportPage}><Download size={15}/>导出当前页</Button></div>
        <p className="data-scroll-hint">左右滑动查看其他字段</p>
        <div className="data-grid-wrap" aria-busy={loading}>
          <table className="data-grid"><thead><tr>{table.columns.map(c=><th key={c.key} aria-sort={query.sort===c.key?(query.direction==='asc'?'ascending':'descending'):'none'}><button disabled={loading} onClick={()=>setQuery(old=>({...old,offset:0,sort:c.key,direction:old.sort===c.key&&old.direction==='asc'?'desc':'asc'}))}>{c.label}{query.sort===c.key?(query.direction==='asc'?<ArrowUp size={13}/>:<ArrowDown size={13}/>):null}</button></th>)}</tr></thead><tbody>{(data?.table.id===selected?data.rows:[]).map((row,i)=><tr key={(data?.offset||0)+i}>{table.columns.map(c=><td key={c.key} className={typeof row[c.key]==='number'?'numeric':''}>{dataCell(row[c.key],c)}</td>)}</tr>)}</tbody></table>
          {!loading&&data&&!data.rows.length&&<div className="data-empty">没有匹配的记录，试试调整筛选条件。</div>}
        </div>
        {data&&data.table.id===selected&&<footer className="data-pagination"><span>{data.matched_rows?`${data.offset+1}–${data.offset+data.rows.length} / ${data.matched_rows.toLocaleString()}`:'0 条记录'}<small>每页 {data.limit} 条 · 直接读取数据表，不消耗模型 Token</small></span><div><Button variant="secondary" disabled={loading||data.offset===0} onClick={()=>setQuery(old=>({...old,offset:0}))}>首页</Button><Button variant="secondary" disabled={loading||data.offset===0} onClick={()=>setQuery(old=>({...old,offset:Math.max(0,data.offset-data.limit)}))}><ChevronLeft size={16}/>上一页</Button><Button variant="secondary" disabled={loading||data.offset+data.limit>=data.matched_rows||data.offset>=MAX_OFFSET} onClick={()=>setQuery(old=>({...old,offset:Math.min(MAX_OFFSET,data.offset+data.limit)}))}>下一页<ChevronRight size={16}/></Button><Button variant="secondary" disabled={loading||data.offset+data.limit>=data.matched_rows||data.offset>=MAX_OFFSET} onClick={()=>setQuery(old=>({...old,offset:Math.min(MAX_OFFSET,Math.max(0,Math.ceil(data.matched_rows/data.limit)-1)*data.limit)}))}>末页</Button></div></footer>}
        {data&&data.matched_rows>MAX_OFFSET+data.limit&&<p className="capability-note">当前最多浏览前 {(MAX_OFFSET+data.limit).toLocaleString()} 条匹配记录。请缩小筛选范围后查看其余记录。</p>}
        <details className="data-origin"><summary>数据来源与使用范围</summary><p>{table.scope==='demo'?'此数据集用于查询与分析验收，不代表客户生产数据或生产实绩。':table.scope==='public'?'来自公开资料，适用范围以原始发布说明为准。':'由管理员登记的数据表，访问范围随对应连接权限控制。'} 当前仅展示管理员开放的表与字段；MCP 接通不代表该系统所有数据都会自动出现。</p></details>
      </div>
    </>}
  </section>;
}
