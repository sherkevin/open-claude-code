import { useEffect, useId, useRef, useState } from 'react';
import { ChevronDown, Clock3, RefreshCw, Search, Sparkles, UserRound, Wrench } from 'lucide-react';
import { api, errorText } from './api';
import { Button, EmptyState, ErrorNotice, Loader, Modal } from './components';
import { appendTrajectory, durationText, filterTrajectory, metricValue, trajectoryDetails, trajectoryLabels, trajectoryPath, trajectoryPositions, trajectoryTime, type TrajectoryItem, type TrajectoryKind, type TrajectoryPage } from './sessionObservationData';
import { ContextContent, type ContextControls } from './ContextDialog';
import './session-observability.css';

const kindIcons={input:UserRound,model:Sparkles,tool:Wrench};
const publicStatuses:Record<string,string>={succeeded:'已完成',completed:'已完成',failed:'失败',running:'进行中',started:'开始',cancelled:'已取消',interrupted:'已中断',pending:'等待中',received:'已接收'};

export function TrajectoryContent({page,items,loading,error,onMore,onRefresh}:{page:TrajectoryPage|null;items:TrajectoryItem[];loading:boolean;error:string;onMore:()=>void;onRefresh:()=>void}) {
  const [query,setQuery]=useState(''),[kind,setKind]=useState<TrajectoryKind|'all'>('all');
  const [expanded,setExpanded]=useState<Set<string>>(new Set());
  const rows=useRef(new Map<string,HTMLElement>());
  const visible=filterTrajectory(items,query,kind);
  const positions=trajectoryPositions(items);
  const summary=page?.summary;
  const select=(item:TrajectoryItem)=>{setKind('all');setQuery('');setExpanded(old=>new Set(old).add(item.id));window.setTimeout(()=>rows.current.get(item.id)?.scrollIntoView({block:'nearest',behavior:'smooth'}),0);};
  const toggle=(id:string)=>setExpanded(old=>{const next=new Set(old);if(next.has(id))next.delete(id);else next.add(id);return next;});
  return <div className="session-trajectory">
    <header className="trajectory-overview"><div><span><Clock3 size={14}/>记录时段 <strong>{durationText(summary?.started_at,summary?.ended_at)}</strong></span><span>轮次 <strong>{metricValue(summary?.turn_count??summary?.input_count,'count')}</strong></span><span>模型请求 <strong>{metricValue(summary?.request_count,'count')}</strong></span><span>工具调用 <strong>{metricValue(summary?.tool_call_count,'count')}</strong></span></div><Button variant="ghost" busy={loading} aria-label="刷新内容轨迹" onClick={onRefresh}><RefreshCw size={14}/></Button></header>
    <ErrorNotice message={error}/>
    <div className="trajectory-lanes" aria-label="输入、模型、工具三泳道">{(['input','model','tool'] as const).map(lane=><div className={`trajectory-lane ${lane}`} key={lane}><span>{trajectoryLabels[lane]}</span><div>{positions.filter(value=>value.item.kind===lane).map(({item,position})=><button type="button" style={{left:`${position}%`}} key={item.id} aria-label={`定位${trajectoryLabels[lane]}：${item.title}`} title={`${trajectoryTime(item.at)} · ${item.title}`} onClick={()=>select(item)}/>)}</div></div>)}</div>
    <div className="trajectory-filters"><label className="search-field"><Search size={16}/><input value={query} onChange={event=>setQuery(event.target.value)} aria-label="搜索已加载的内容轨迹" placeholder="搜索已加载的内容"/></label><div className="trajectory-kind-tabs" aria-label="轨迹类型">{(['all','input','model','tool'] as const).map(value=><button type="button" className={kind===value?'selected':''} aria-pressed={kind===value} onClick={()=>setKind(value)} key={value}>{value==='all'?'全部':trajectoryLabels[value]}</button>)}</div></div>
    <p className="trajectory-scope">已加载 {items.length} 条公开事件{query||kind!=='all'?` · 当前显示 ${visible.length} 条`:''}{page?.has_more?' · 可继续加载更多记录':''}。模型记录展示公开回答，工具记录展示执行摘要。</p>
    {loading&&!items.length?<Loader text="正在读取内容轨迹…"/>:!visible.length?<EmptyState icon={<Search size={26}/>} title={query||kind!=='all'?'没有匹配的记录':'还没有内容轨迹'} description={query||kind!=='all'?'试试其他关键词，或加载更多记录。':'发送消息后，可在这里查看输入、公开回答与工具执行摘要。'}/>:<div className="trajectory-list">{visible.map(item=>{
      const Icon=kindIcons[item.kind]; const open=expanded.has(item.id); const details=trajectoryDetails(item.details);
      return <article className={`trajectory-item ${item.kind}`} key={item.id} ref={node=>{if(node)rows.current.set(item.id,node);else rows.current.delete(item.id);}}>
        <button type="button" className="trajectory-item-heading" aria-expanded={open} onClick={()=>toggle(item.id)}><span className="trajectory-icon"><Icon size={17}/></span><span className="trajectory-item-main"><span><strong>{item.title||trajectoryLabels[item.kind]}</strong><small>{publicStatuses[item.status]||''}</small></span><p>{item.text||'展开查看记录信息'}</p></span><time>{trajectoryTime(item.at)}</time><ChevronDown size={16} className={open?'expanded':''}/></button>
        {open&&<div className="trajectory-item-body">{!!details.length&&<dl>{details.map(detail=><div key={detail.label}><dt>{detail.label}</dt><dd>{detail.value}</dd></div>)}</dl>}{item.text?<p>{item.text}</p>:<p className="trajectory-muted">这条事件没有额外的公开内容。</p>}{item.text_truncated&&<small>当前记录为内容摘要。</small>}</div>}
      </article>;
    })}</div>}
    {page?.has_more&&<div className="trajectory-load-more"><Button variant="secondary" busy={loading} onClick={onMore}>加载更多记录</Button></div>}
  </div>;
}

function TrajectoryPanel({conversationId}:{conversationId:string}) {
  const [page,setPage]=useState<TrajectoryPage|null>(null),[items,setItems]=useState<TrajectoryItem[]>([]),[loading,setLoading]=useState(true),[error,setError]=useState(''),[revision,setRevision]=useState(0);
  const controller=useRef<AbortController|null>(null),version=useRef(0);
  useEffect(()=>{
    const request=new AbortController();controller.current=request;const current=++version.current;
    setLoading(true);setError('');setPage(null);setItems([]);
    void api<TrajectoryPage>(trajectoryPath(conversationId),{signal:request.signal}).then(value=>{if(current===version.current&&!request.signal.aborted){setPage(value);setItems(value.items);}}).catch(cause=>{if(!request.signal.aborted&&current===version.current)setError(errorText(cause));}).finally(()=>{if(!request.signal.aborted&&current===version.current)setLoading(false);});
    return()=>{request.abort();controller.current?.abort();};
  },[conversationId,revision]);
  const more=async()=>{
    if(loading||!page?.has_more||!page.next_cursor)return;
    const current=version.current;const request=new AbortController();controller.current=request;setLoading(true);setError('');
    try{const value=await api<TrajectoryPage>(trajectoryPath(conversationId,page.next_cursor),{signal:request.signal});if(current===version.current&&!request.signal.aborted){setItems(previous=>appendTrajectory(previous,value.items));setPage(value);}}
    catch(cause){if(current===version.current&&!request.signal.aborted)setError(errorText(cause));}
    finally{if(current===version.current&&!request.signal.aborted)setLoading(false);}
  };
  return <div className="modal-body trajectory-modal-body"><TrajectoryContent key={conversationId} page={page} items={items} loading={loading} error={error} onMore={()=>void more()} onRefresh={()=>setRevision(value=>value+1)}/></div>;
}

export type InspectionView = 'trajectory' | 'context';

export default function SessionTrajectory({ conversationId, title, onClose, context, initialView = 'trajectory', onViewChange }: {
  conversationId: string;
  title: string;
  onClose: () => void;
  context?: ContextControls;
  initialView?: InspectionView;
  onViewChange?: (view: InspectionView) => void;
}) {
  const [view, setView] = useState<InspectionView>(context ? initialView : 'trajectory');
  const [visited, setVisited] = useState(() => new Set([view]));
  const [compacting, setCompacting] = useState(false);
  const tabs = useRef<HTMLDivElement>(null);
  const id = useId();
  const select = (next: InspectionView) => {
    if (compacting) return;
    setView(next);
    setVisited(previous => previous.has(next) ? previous : new Set([...previous, next]));
    onViewChange?.(next);
  };
  return <Modal title={`${title || '会话'} · 轨迹`} wide onClose={() => { if (!compacting) onClose(); }}>
    {context && <div className="inspection-tabs" ref={tabs} role="tablist" aria-label="轨迹视图">
      {(['trajectory', 'context'] as const).map(key => <button key={key} id={`${id}-${key}`} type="button" role="tab"
        aria-controls={`${id}-panel-${key}`} aria-selected={view === key} tabIndex={view === key ? 0 : -1}
        disabled={compacting && view !== key} onClick={() => select(key)} onKeyDown={event => {
          if (compacting || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
          event.preventDefault();
          const next = event.key === 'Home' ? 'trajectory' : event.key === 'End' ? 'context' : key === 'trajectory' ? 'context' : 'trajectory';
          select(next);
          tabs.current?.querySelector<HTMLButtonElement>(`button[data-view="${next}"]`)?.focus();
        }} data-view={key}>{key === 'trajectory' ? '执行轨迹' : '上下文'}</button>)}
    </div>}
    <div className="inspection-panel" hidden={view !== 'trajectory'} id={`${id}-panel-trajectory`} role={context ? 'tabpanel' : undefined} aria-labelledby={context ? `${id}-trajectory` : undefined}>
      {visited.has('trajectory') && <TrajectoryPanel conversationId={conversationId}/>}
    </div>
    {context && <div className="inspection-panel" hidden={view !== 'context'} id={`${id}-panel-context`} role="tabpanel" aria-labelledby={`${id}-context`}>
      {visited.has('context') && <ContextContent conversationId={conversationId} {...context} onCompacting={setCompacting}/>}
    </div>}
    <footer className="modal-footer"><Button variant="secondary" disabled={compacting} onClick={onClose}>关闭</Button></footer>
  </Modal>;
}
