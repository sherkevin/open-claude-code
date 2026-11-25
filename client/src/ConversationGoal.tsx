import {useEffect,useRef,useState} from 'react';
import {Target} from 'lucide-react';
import {api,errorText,post} from './api';
import {Button,ErrorNotice,Field,Loader,Modal} from './components';

export type NativeGoal={id:string;revision:number;objective:string;phase:'active'|'paused'|'blocked'|'complete';maxGoalRounds:number;roundsStarted:number;createdAt:number;updatedAt:number;activation:'disarmed';blockedReason?:{code:string;message:string}};
type Receipt={goal:NativeGoal|null;continuation_driver:false};
type Action='create'|'edit'|'pause'|'resume'|'complete'|'clear';
const phases={active:'目标已记录',paused:'已暂停',blocked:'存在阻塞',complete:'已标记完成'} as const;
export function goalChange(action:Action,goal:NativeGoal|null,objective:string,rounds:number){
  if(action==='create'||action==='edit'){
    if(!objective.trim()||!Number.isInteger(rounds)||rounds<1||rounds>32)throw new Error('请输入目标并设置 1–32 轮的轮次上限。');
    if(action==='create')return {action,objective:objective.trim(),maxGoalRounds:rounds};
    if(!goal)throw new Error('目标已变化，请刷新。');
    return {action,ref:{id:goal.id,revision:goal.revision},objective:objective.trim(),maxGoalRounds:rounds};
  }
  if(!goal)throw new Error('目标已变化，请刷新。');
  return {action,ref:{id:goal.id,revision:goal.revision}};
}

export function GoalStateView({goal}:{goal:NativeGoal|null}){
  return <div className="section-help">
    <strong>{goal?phases[goal.phase]:'尚未创建目标'}</strong>
    {goal&&<><p>已开始 {goal.roundsStarted} / {goal.maxGoalRounds} 轮</p>{goal.blockedReason&&<p>{goal.blockedReason.message}</p>}</>}
    <p>当前为原生目标状态试点，自动执行驱动尚未启用。保存或恢复目标不会启动任务。</p>
  </div>;
}

function GoalDialog({conversationId,unavailable,onClose}:{conversationId:string;unavailable:boolean;onClose:()=>void}){
  const path='/conversations/'+encodeURIComponent(conversationId)+'/goal';
  const [receipt,setReceipt]=useState<Receipt|null>(null),[objective,setObjective]=useState(''),[rounds,setRounds]=useState(8);
  const [loading,setLoading]=useState(true),[busy,setBusy]=useState(false),[error,setError]=useState(''),[refresh,setRefresh]=useState(0);
  const pending=useRef(false);
  function accept(result:Receipt){
    if(result.continuation_driver!==false)throw new Error('目标试点回执不匹配，请刷新。');
    setReceipt(result);setObjective(result.goal?.objective||'');setRounds(result.goal?.maxGoalRounds||8);
  }
  useEffect(()=>{
    const controller=new AbortController();
    setLoading(true);setReceipt(null);setError('');
    void api<Receipt>(path,{signal:controller.signal,cache:'no-store'}).then(r=>{if(!controller.signal.aborted)accept(r);})
      .catch(e=>{if(!controller.signal.aborted)setError(errorText(e));})
      .finally(()=>{if(!controller.signal.aborted)setLoading(false);});
    return()=>controller.abort();
  },[path,refresh]);
  async function change(action:Action){
    if(pending.current||unavailable||!receipt)return;
    setError('');
    try{
      const payload=goalChange(action,receipt.goal,objective,rounds);
      if(['clear','complete','resume'].includes(action)&&!window.confirm(action==='clear'?'清除当前目标，保留原生历史？':action==='complete'?'确认目标已达到完成条件？':'恢复目标状态？当前不会自动执行。'))return;
      pending.current=true;setBusy(true);
      const result=await post<Receipt>(path,payload);accept(result);
    }catch(e){
      setError(errorText(e)+' 请刷新状态后再提交，避免重复操作。');
      setReceipt(null);
    }finally{pending.current=false;setBusy(false);}
  }
  const goal=receipt?.goal;
  return <Modal title="持续目标（状态试点）" wide onClose={()=>{if(!pending.current)onClose();}}>
    <div className="modal-body">
      <ErrorNotice message={error}/>
      {unavailable&&<p className="section-help">当前会话不可修改目标，请先结束正在运行的任务。</p>}
      {loading?<Loader text="正在读取原生目标…"/>:receipt&&<>
        <GoalStateView goal={goal||null}/>
        <Field label="目标与完成条件"><textarea rows={6} value={objective} maxLength={8000} disabled={busy||unavailable} onChange={e=>setObjective(e.target.value)}/></Field>
        <Field label="最多目标轮次" hint="此处只保存轮次限制，不代表 token、金额或时间预算。"><input type="number" min={1} max={32} value={rounds} disabled={busy||unavailable} onChange={e=>setRounds(Number(e.target.value))}/></Field>
        <div className="task-sidebar-actions">
          <Button busy={busy} disabled={unavailable||!objective.trim()} onClick={()=>void change(goal?'edit':'create')}>{goal?'保存目标修改':'创建目标'}</Button>
          {goal&&goal.phase==='active'&&<Button variant="secondary" disabled={busy||unavailable} onClick={()=>void change('pause')}>暂停</Button>}
          {goal&&['paused','blocked'].includes(goal.phase)&&<Button variant="secondary" disabled={busy||unavailable} onClick={()=>void change('resume')}>恢复状态</Button>}
          {goal&&goal.phase!=='complete'&&<Button variant="secondary" disabled={busy||unavailable} onClick={()=>void change('complete')}>标记完成</Button>}
          {goal&&<Button variant="danger" disabled={busy||unavailable} onClick={()=>void change('clear')}>清除目标</Button>}
        </div>
      </>}
    </div>
    <footer className="modal-footer"><Button variant="secondary" disabled={busy||loading} onClick={()=>setRefresh(v=>v+1)}>刷新状态</Button><Button variant="secondary" disabled={busy} onClick={onClose}>关闭</Button></footer>
  </Modal>;
}

export default function ConversationGoal({conversationId,unavailable}:{conversationId:string;unavailable:boolean}){
  const [open,setOpen]=useState(false);
  return <><button className="icon-button" type="button" title="持续目标（状态试点）" aria-label="持续目标" disabled={unavailable} onClick={()=>setOpen(true)}><Target size={17}/></button>
    {open&&<GoalDialog key={conversationId} conversationId={conversationId} unavailable={unavailable} onClose={()=>setOpen(false)}/>}</>;
}
