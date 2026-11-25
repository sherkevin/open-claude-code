import { useEffect,useRef,useState } from 'react';
import { CalendarClock,RefreshCw } from 'lucide-react';
import { api,errorText,remove } from './api';
import { Button,EmptyState,ErrorNotice,Loader,PageHeader } from './components';
import { ScheduleRow,changeScheduleLifecycle,scheduleLifecycleConfirmation,type Schedule } from './ConversationTasks';
import type { Conversation } from './types';

export type CatalogSchedule=Schedule & {
  conversation_id:string;conversation_title:string;conversation_archived:boolean|number;
  kind:'series'|'once'|'occurrence';run_id?:string|null;message_id?:string|null;
};
type Catalog={items:CatalogSchedule[];next_cursor:string|null};
type Scope='active'|'history';

export function catalogPath(scope:Scope,before?:string|null){
  const query=new URLSearchParams({scope,limit:'30'});
  if(before)query.set('before',before);
  return '/schedules?'+query.toString();
}

export function scopedSchedulePath(task:CatalogSchedule){
  return '/conversations/'+encodeURIComponent(task.conversation_id)+'/schedules';
}

export default function SchedulesPage({onOpen}:{onOpen:(conversation:Conversation,messageId?:string)=>void}){
  const [scope,setScope]=useState<Scope>('active'),[page,setPage]=useState<Catalog|null>(null),[loading,setLoading]=useState(true),[busy,setBusy]=useState(false),[error,setError]=useState(''),[reload,setReload]=useState(0);
  const lock=useRef(false), request=useRef<AbortController|null>(null);
  const refresh=()=>setReload(v=>v+1);
  useEffect(()=>{
    const controller=new AbortController();request.current=controller;
    setPage(null);setLoading(true);setError('');
    void api<Catalog>(catalogPath(scope),{signal:controller.signal,cache:'no-store'}).then(result=>{
      if(!controller.signal.aborted)setPage(result);
    }).catch(err=>{
      if(!controller.signal.aborted)setError(errorText(err));
    }).finally(()=>{
      if(!controller.signal.aborted)setLoading(false);
    });
    return()=>{controller.abort();};
  },[scope,reload]);
  async function loadMore(){
    if(lock.current||loading||!page?.next_cursor)return;
    const controller=request.current;
    lock.current=true;setBusy(true);setError('');
    try{
      const result=await api<Catalog>(catalogPath(scope,page.next_cursor),{signal:controller?.signal,cache:'no-store'});
      if(controller?.signal.aborted)return;
      setPage(previous=>previous?{items:[...previous.items,...result.items.filter(row=>!previous.items.some(old=>old.id===row.id))],next_cursor:result.next_cursor}:result);
    }catch(e){if(!controller?.signal.aborted)setError(errorText(e));}
    finally{lock.current=false;setBusy(false);}
  }
  async function act(action:()=>Promise<void>){
    if(lock.current||loading)return;
    lock.current=true;setBusy(true);setError('');
    try{await action();refresh();}
    catch(e){setError(errorText(e));}
    finally{lock.current=false;setBusy(false);}
  }
  function lifecycle(task:CatalogSchedule,action:'pause'|'resume'){
    if(lock.current||!window.confirm(scheduleLifecycleConfirmation(action)))return;
    void act(async()=>{await changeScheduleLifecycle(scopedSchedulePath(task),task.id,action);});
  }
  function cancel(task:CatalogSchedule){
    if(lock.current||!window.confirm(task.kind==='series'?'取消后不再执行未来周期；已启动任务不受影响。确认取消？':'取消尚未启动的任务？'))return;
    void act(async()=>{await remove(scopedSchedulePath(task)+'/'+encodeURIComponent(task.id));});
  }
  function open(task:CatalogSchedule){
    void act(async()=>{
      const conversation=await api<Conversation>('/conversations/'+encodeURIComponent(task.conversation_id));
      if(conversation.id!==task.conversation_id)throw new Error('会话身份不一致，请刷新');
      onOpen(conversation,task.message_id||undefined);
    });
  }
  return <div className="page">
    <PageHeader eyebrow="自动化" title="定时任务中心" description="集中查看和管理你在所有会话创建的定时任务。" actions={<Button variant="secondary" busy={loading} disabled={busy} onClick={refresh}><RefreshCw size={16}/>刷新</Button>}/>
    <div className="checkbox-row" role="group" aria-label="任务范围">
      <Button variant={scope==='active'?'primary':'secondary'} disabled={busy} onClick={()=>setScope('active')}>活动计划与任务</Button>
      <Button variant={scope==='history'?'primary':'secondary'} disabled={busy} onClick={()=>setScope('history')}>执行历史</Button>
    </div>
    <p className="section-help">只显示本人会话的任务。计划状态与实际运行状态分开；创建新任务请进入对应会话的右侧定时任务区域。</p>
    <ErrorNotice message={error}/>
    {loading?<Loader text="正在读取定时任务…"/>:!page?null:!page.items.length?<EmptyState icon={<CalendarClock size={30}/>} title={scope==='active'?'暂无活动计划与任务':'暂无执行历史'} description="在业务会话中安排定时任务后，记录会显示在这里。"/>:<ul className="task-sidebar-list" aria-label={scope==='active'?'活动定时任务':'定时执行历史'}>
      {page.items.map(task=><li key={task.id} className="approval-card">
        <div className="approval-title"><CalendarClock size={16}/><strong>{task.conversation_title||'新会话'}</strong><span>{task.kind==='series'?'周期计划':task.kind==='occurrence'?'周期执行记录':'单次任务'}{task.conversation_archived?' · 已归档':''}</span></div>
        <ul className="task-sidebar-list"><ScheduleRow task={task} busy={busy} onCancel={()=>cancel(task)} onLifecycle={(_,action)=>lifecycle(task,action)}/></ul>
        <Button variant="ghost" disabled={busy} onClick={()=>open(task)}>{task.message_id?'查看执行轮次':'打开关联会话'}</Button>
      </li>)}
    </ul>}
    {page?.next_cursor&&<Button variant="secondary" busy={busy} disabled={loading} onClick={()=>void loadMore()}>加载更多</Button>}
  </div>;
}
