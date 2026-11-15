import {useEffect,useState} from 'react';
import {Activity,RefreshCw} from 'lucide-react';
import {api,errorText} from './api';
import {Button,EmptyState,ErrorNotice,Loader,PageHeader} from './components';
import type {UsageSummary,UsageTotals,UsageField} from './types';
import './usage.css';
export interface UsageFilters {scope:'self'|'all';model_id:string;date_from:string;date_to:string}
export const usageQuery=(filters:UsageFilters,admin:boolean)=>{
  const params=new URLSearchParams({scope:admin?filters.scope:'self'});
  for(const key of ['model_id','date_from','date_to'] as const) if(filters[key]) params.set(key,filters[key]);
  return `/usage?${params}`;
};
const emptyFilters:UsageFilters={scope:'self',model_id:'',date_from:'',date_to:''};
const calls=(item:UsageTotals)=>item.model_call_count==null?`已知 ${item.known_model_calls??0}`:String(item.model_call_count);
const kinds:Record<string,string>={chat:'业务问答',compact:'上下文压缩',completion:'输入补全',routing:'自动选模',followups:'后续问题',knowledge_vision:'知识图片解析'};
const tokenFields:UsageField[]=['input_tokens','output_tokens','cache_read_input_tokens','cache_creation_input_tokens'];
export function usageDisplay(item:UsageTotals,field:UsageField):string {
  const amount=item[field],unknown=item.field_coverage?.[field]?.unknown_runs??0;
  if(typeof amount==='number'&&Number.isFinite(amount)&&!unknown) return field==='estimated_cost_usd'?`$${amount.toFixed(4)}`:amount.toLocaleString('zh-CN');
  if(field==='estimated_cost_usd') return '未提供';
  const partial=item.known_subtotals?.[field],reported=item.field_coverage?.[field]?.known_runs??0;
  return typeof partial==='number'&&Number.isFinite(partial)&&(partial>0||reported>0)?partial.toLocaleString('zh-CN'):'未报告';
}
export function usageDetail(item:UsageTotals,field:UsageField):string {
  const coverage=item.field_coverage?.[field];
  if(!coverage)return '报告范围未提供';
  if(coverage.unknown_runs>0) {
    if(field==='estimated_cost_usd') return `${coverage.unknown_runs} 条记录未报告费用，无法计算完整金额。`;
    const partial=item.known_subtotals?.[field];
    if(!coverage.known_runs&&(!partial||!Number.isFinite(partial))) return `${coverage.unknown_runs} 条记录未报告用量。`;
    return `已记录小计；另有 ${coverage.unknown_runs} 条记录未完整报告。`;
  }
  return `${coverage.known_runs} 条记录已完整报告。`;
}
const tokenNames=['输入 Token','输出 Token','缓存读取 Token','缓存写入 Token'];
export function UsageReport({usage}:{usage:UsageSummary}){
  const [group,setGroup]=useState<'model'|'day'>('model');
  const rows=group==='model'?(usage.by_model||[]).map(i=>({...i,key:i.model_id,label:i.model_label})):(usage.by_day||[]).map(i=>({...i,key:i.day,label:i.day}));
  const count=usage.usage_records??usage.total_runs;
  return <><p className="usage-scope-note">{usage.scope==='all'?'全体成员':'仅我的账号'} · 北京时间（UTC+8） · {usage.date_from||'最早记录'} 至 {usage.date_to||'现在'}</p>
    {!count?<EmptyState icon={<Activity size={28}/>} title="当前范围暂无用量记录" description="可调整日期、模型或账号范围。开始业务问答或使用输入补全后，这里会显示已记录的调用。"/>:<>
      <div className="usage-grid"><div className="metric-card"><div className="metric-label">模型调用</div><div className="metric-value">{calls(usage)}<small>次</small></div><p>{usage.unknown_call_records?`另有 ${usage.unknown_call_records} 条记录的完整调用次数未知。`:'包含业务问答、输入补全、选模和后续问题。'}</p></div><div className="metric-card"><div className="metric-label">业务任务</div><div className="metric-value">{usage.total_runs}<small>次</small></div><p>{usage.successful_runs} 次成功完成；另有 {usage.auxiliary_calls??0} 次辅助调用。</p></div>{tokenFields.map((field,i)=><div className="metric-card" key={field}><div className="metric-label">{tokenNames[i]}</div><div className="metric-value">{usageDisplay(usage,field)}</div><p>{usageDetail(usage,field)}</p></div>)}</div>
      <div className="cost-panel"><div><span className="eyebrow">费用估算 · {usage.currency}</span><h2>{usageDisplay(usage,'estimated_cost_usd')}</h2><small className="usage-coverage">{usageDetail(usage,'estimated_cost_usd')}</small></div><p>{usage.cost_note||'未报告价格或费用时不换算金额；服务估算不能代替实际账单。'}</p></div>
      <section className="usage-breakdown"><div className="section-label"><span>用量明细</span><div className="usage-group-switch" role="group" aria-label="用量分组"><button type="button" aria-pressed={group==='model'} onClick={()=>setGroup('model')}>按模型</button><button type="button" aria-pressed={group==='day'} onClick={()=>setGroup('day')}>按日期</button></div></div><div className="table-container"><table><thead><tr><th>{group==='model'?'模型':'日期（北京时间）'}</th><th>模型调用</th>{tokenNames.map(name=><th key={name}>{name}</th>)}</tr></thead><tbody>{rows.map(item=><tr key={item.key}><td>{item.label}</td><td>{calls(item)}{Boolean(item.unknown_call_records)&&<small className="usage-coverage">另有 {item.unknown_call_records} 条记录次数未知</small>}</td>{tokenFields.map(field=><td key={field}>{usageDisplay(item,field)}<small className="usage-coverage">{usageDetail(item,field)}</small></td>)}</tr>)}</tbody></table></div></section>
      {!!usage.by_kind?.length&&<p className="usage-kind-note">{usage.by_kind.map(i=>`${kinds[i.kind]||'其他调用'}：${i.usage_records??i.total_runs} 条记录`).join(' · ')}</p>}
      {usage.scope==='all'&&<section className="member-usage"><div className="section-label"><span>成员用量</span><span>{usage.users.length} 位成员</span></div><div className="table-container"><table><thead><tr><th>成员</th><th>模型调用</th><th>业务任务</th><th>输入 Token</th><th>输出 Token</th></tr></thead><tbody>{usage.users.map((item,i)=><tr key={item.user_id||item.id||i}><td>{item.display_name||item.username||'成员'}</td><td>{calls(item)}</td><td>{item.total_runs}</td>{tokenFields.slice(0,2).map(field=><td key={field}>{usageDisplay(item,field)}<small className="usage-coverage">{usageDetail(item,field)}</small></td>)}</tr>)}</tbody></table></div></section>}
    </>}<p className="usage-history-note">{usage.auxiliary_history_note||'未完整报告的用量保留为未知，不按零计算。'} 缓存复用不重复计入模型调用。</p></>;
}
export function UsagePage({admin}:{admin:boolean}){
  const [usage,setUsage]=useState<UsageSummary|null>(null),[error,setError]=useState(''),[busy,setBusy]=useState(true);
  const [filters,setFilters]=useState<UsageFilters>(emptyFilters),[applied,setApplied]=useState<UsageFilters>(emptyFilters),[revision,setRevision]=useState(0);
  const [models,setModels]=useState<Array<{id:string;label:string}>>([]); const query=usageQuery(applied,admin);
  useEffect(()=>{const controller=new AbortController();setBusy(true);setError('');setUsage(null);
    void api<UsageSummary>(query,{signal:controller.signal}).then(value=>{if(!controller.signal.aborted){setUsage(value);setModels(value.available_models||[]);}}).catch(err=>{if(!controller.signal.aborted)setError(errorText(err));}).finally(()=>{if(!controller.signal.aborted)setBusy(false);});
    return ()=>controller.abort();
  },[query,revision]);
  return <div className="page usage-page"><PageHeader eyebrow="账号与用量" title="用量记录" description="查看实际调用与已报告的 Token 用量。" actions={<Button variant="secondary" busy={busy} onClick={()=>setRevision(v=>v+1)}><RefreshCw size={16}/>刷新</Button>}/>
    <form className="usage-filters" onSubmit={e=>{e.preventDefault();if(filters.date_from&&filters.date_to&&filters.date_from>filters.date_to){setError('开始日期不能晚于结束日期。');return;}setApplied({...filters});setRevision(v=>v+1);}}>
      {admin&&<label>账号范围<select value={filters.scope} onChange={e=>setFilters({...filters,scope:e.target.value as 'self'|'all'})}><option value="self">仅我的账号</option><option value="all">全体成员</option></select></label>}
      <label>模型<select value={filters.model_id} onChange={e=>setFilters({...filters,model_id:e.target.value})}><option value="">全部模型</option>{models.map(m=><option key={m.id} value={m.id}>{m.label}</option>)}{filters.model_id&&!models.some(m=>m.id===filters.model_id)&&<option value={filters.model_id}>当前选择的模型</option>}</select></label>
      <label>开始日期<input type="date" value={filters.date_from} max={filters.date_to||undefined} onChange={e=>setFilters({...filters,date_from:e.target.value})}/></label><label>结束日期<input type="date" value={filters.date_to} min={filters.date_from||undefined} onChange={e=>setFilters({...filters,date_to:e.target.value})}/></label>
      <Button type="submit" disabled={busy}>查询</Button><Button type="button" variant="ghost" disabled={busy} onClick={()=>{setFilters(emptyFilters);setApplied(emptyFilters);setRevision(v=>v+1);}}>重置</Button><small>日期按北京时间统计</small>
    </form><ErrorNotice message={error}/>{busy?<Loader text="正在读取用量…"/>:usage?<UsageReport usage={usage}/>:<EmptyState icon={<Activity size={28}/>} title="暂时无法读取用量" description="请点击刷新重试。"/>}</div>;
}
