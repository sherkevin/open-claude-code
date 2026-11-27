import {useEffect,useRef,useState} from 'react';
import {Target} from 'lucide-react';
import {api,errorText,post} from './api';
import {Button,ErrorNotice,Field,Loader,Modal} from './components';

export type NativeGoal={id:string;revision:number;objective:string;phase:'active'|'paused'|'blocked'|'complete';maxGoalRounds:number;roundsStarted:number;createdAt:number;updatedAt:number;activation:'disarmed';blockedReason?:{code:string;message:string}};
type Receipt={goal:NativeGoal|null;continuation_driver:false};
type Action='create'|'edit'|'pause'|'resume'|'complete'|'clear';
export type GoalExecutionLimits={maxRounds:number;tokenBudget:number;costBudgetMicros:number;roundCostBudgetMicros?:number;timeBudgetSeconds:number};
type ExecutionAuthorization={id:string;goal_id:string;goal_revision:number;harness_session_id:string;max_rounds:number;token_budget:number;cost_budget_micros:number;round_cost_budget_micros?:number|null;time_budget_seconds:number;status:'active'|'revoked';revoked_reason?:string|null;expires_at:string|null};
type ExecutionBudget={rounds_used:number;rounds_executed:number;usage_complete:boolean;cost_authoritative:boolean;tokens_used:number|null;cost_micros_used:number|null;synthetic_rounds:number;unknown_rounds:number[];rounds_remaining:number|null;tokens_remaining:number|null;cost_micros_remaining:number|null;time_remaining_seconds:number|null};
type CostAttempt = {
  attempt_id: string;
  kind: 'parent' | 'child' | 'retry';
  parent_attempt_id: string | null;
  reserved_micros: number;
  settled_micros: number | null;
  state: 'reserved' | 'started' | 'settled' | 'unused';
};
type CostReservation = {source:string;reserved_micros:number;settled_micros:number|null;attempts:CostAttempt[]};
type ExecutionRound={cost_reservation?:CostReservation|null;id:string;round_index:number;state:'admitted'|'settled'|'aborted';armed:boolean;usage_status:'complete'|'unknown'|null;usage_source?:string|null;cost_authoritative:boolean|null;total_tokens:number|null;cost_micros:number|null;run_id:string;assistant_message_id:string|null;error_code?:string|null;abort_reason?:string|null};
export type GoalExecution={harness_session_id:string|null;authorization:ExecutionAuthorization|null;budget:ExecutionBudget|null;active_lease:{id:string;round_index:number;armed:boolean}|null;rounds:ExecutionRound[];can_start:boolean;driver_integrated:boolean;single_round_per_run:true;unattended_execution:false;continuation_driver:false};
const phases={active:'目标已记录',paused:'已暂停',blocked:'存在阻塞',complete:'已标记完成'} as const;
const positive=(value:number,min:number,max:number)=>Number.isSafeInteger(value)&&value>=min&&value<=max;
/** Build the exact explicit approval payload; never infer budgets from rounds. */
export function goalExecutionAuthorization(goal:NativeGoal|null,sessionId:string|null,limits:GoalExecutionLimits){
  if(!goal||!sessionId)throw new Error('请先读取当前目标的原生会话，再授权执行。');
  if(!positive(limits.maxRounds,1,32)||!positive(limits.tokenBudget,1,1_000_000_000_000)
    ||!positive(limits.costBudgetMicros,1,1_000_000_000_000)||!positive(limits.timeBudgetSeconds,60,30*24*3600))
    throw new Error('请设置有效的轮次、token、费用和时间预算。');
  const perRound=limits.roundCostBudgetMicros;
  if(perRound!==undefined && !positive(perRound,1,limits.costBudgetMicros))
    throw new Error('单轮费用上限须为正整数，且不超过总费用预算。');
  return {goal_id:goal.id,goal_revision:goal.revision,harness_session_id:sessionId,
    ...(perRound===undefined?{}:{round_cost_budget_micros:perRound}),
    max_rounds:limits.maxRounds,token_budget:limits.tokenBudget,
    cost_budget_micros:limits.costBudgetMicros,time_budget_seconds:limits.timeBudgetSeconds,confirm:true};
}
/** One explicit round. Never sends the objective, model text or a budget. */
export function goalRoundStart(goal:NativeGoal|null){
  if(!goal)throw new Error('目标已变化，请刷新。');
  return {goal_id:goal.id,goal_revision:goal.revision,confirm:true};
}
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

export function GoalStateView({goal,executionEnabled=false}:{goal:NativeGoal|null;executionEnabled?:boolean}){
  return <div className="section-help">
    <strong>{goal?phases[goal.phase]:'尚未创建目标'}</strong>
    {goal&&<><p>已开始 {goal.roundsStarted} / {goal.maxGoalRounds} 轮</p>{goal.blockedReason&&<p>{goal.blockedReason.message}</p>}</>}
    <p>{executionEnabled?'执行驱动已接入：每次开始一轮都由你明确确认，不会无人值守自动续跑。保存或恢复目标本身不会启动任务。':'当前为原生目标状态试点，自动执行驱动尚未启用。保存或恢复目标不会启动任务。'}</p>
  </div>;
}

const micros=(value:number|null|undefined)=>value==null?'未知':`${value.toLocaleString('zh-CN')} 微美元`;
const attemptNames={parent:'主任务',child:'子任务',retry:'重试'} as const;
const attemptStates={reserved:'已预留',started:'已开始',settled:'已结算',unused:'未使用'} as const;

function RoundCosts({reservation}:{reservation:CostReservation}){
  return <details>
    <summary>逐笔费用 · 预留 {micros(reservation.reserved_micros)} · 结算 {micros(reservation.settled_micros)}</summary>
    {reservation.source==='loopback_test'&&<p>计量来源：隔离回环测试。</p>}
    <ul>{reservation.attempts.map(attempt=><li key={attempt.attempt_id}>
      {attemptNames[attempt.kind]} · {attemptStates[attempt.state]} · 预留 {micros(attempt.reserved_micros)} · 结算 {micros(attempt.settled_micros)}
    </li>)}</ul>
  </details>;
}
/** Read-only execution accounting; synthetic and production billing stay separate. */
export function GoalExecutionView({execution,goal}:{execution:GoalExecution;goal:NativeGoal|null}){
  const authorization=execution.authorization,budget=execution.budget;
  return <div className="section-help">
    <strong>目标执行</strong>
    {!authorization&&<p>尚未授权执行。授权绑定当前目标内容和原生会话，只允许你逐轮明确启动。</p>}
    {authorization&&<p>{authorization.status==='active'?'执行授权生效中':'执行授权已撤销'}{authorization.revoked_reason?`（${authorization.revoked_reason}）`:''}</p>}
    {authorization?.round_cost_budget_micros!=null&&<p>已授权的单轮费用上限：{micros(authorization.round_cost_budget_micros)}</p>}
    {budget&&<p>轮次已用 {budget.rounds_used}（已执行 {budget.rounds_executed}），剩余 {budget.rounds_remaining??'未知'}；token 已用 {budget.tokens_used??'未知'}，剩余 {budget.tokens_remaining??'未知'}；费用已用 {micros(budget.cost_micros_used)}，剩余 {micros(budget.cost_micros_remaining)}；时间剩余 {budget.time_remaining_seconds??'未知'} 秒。</p>}
    {budget&&!budget.usage_complete&&<p role="alert">前序轮次用量未完整确认。系统不会把未知当零，下一轮不能开始。</p>}
    {budget&&budget.synthetic_rounds>0&&<p>其中 {budget.synthetic_rounds} 轮为回环合成计费，不是生产权威计费。</p>}
    {budget&&budget.rounds_executed>0&&budget.usage_complete&&budget.cost_authoritative&&<p>费用来自权威回执。</p>}
    {execution.rounds.length>0&&<ol>{execution.rounds.slice(-5).map(item=><li key={item.id}>第 {item.round_index} 轮：{item.state} · 用量 {item.usage_status??'未结算'}{item.error_code?` · ${item.error_code}`:''}
      {item.cost_reservation&&<RoundCosts reservation={item.cost_reservation}/>}
    </li>)}</ol>}
    {goal&&<p>下一轮从原生第 {goal.roundsStarted+1} 轮开始；不会自动续跑。</p>}
  </div>;
}

function GoalDialog({conversationId,unavailable,executionEnabled,onRoundStarted,onClose}:{
  conversationId:string;unavailable:boolean;executionEnabled:boolean;onRoundStarted?:(runId:string)=>void;onClose:()=>void
}){
  const path='/conversations/'+encodeURIComponent(conversationId)+'/goal';
  const execPath=path+'/execution';
  const [receipt,setReceipt]=useState<Receipt|null>(null),[objective,setObjective]=useState(''),[rounds,setRounds]=useState(8);
  const [execution,setExecution]=useState<GoalExecution|null>(null);
  const [limits,setLimits]=useState<GoalExecutionLimits>({maxRounds:2,tokenBudget:10000,costBudgetMicros:10000,timeBudgetSeconds:3600});
  const [loading,setLoading]=useState(true),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const [execError,setExecError]=useState('');
  const [refresh,setRefresh]=useState(0);
  const pending=useRef(false);
  function accept(result:Receipt){
    if(result.continuation_driver!==false)throw new Error('目标试点回执不匹配，请刷新。');
    setReceipt(result);setObjective(result.goal?.objective||'');setRounds(result.goal?.maxGoalRounds||8);
  }
  async function loadExecution(signal?:AbortSignal){
    if(!executionEnabled)return;
    const value=await api<GoalExecution>(execPath,{signal,cache:'no-store'});
    if(!signal?.aborted)setExecution(value);
  }
  useEffect(()=>{
    const controller=new AbortController();
    setLoading(true);setReceipt(null);setExecution(null);setError('');setExecError('');
    if(unavailable)setLoading(false);
    else void api<Receipt>(path,{signal:controller.signal,cache:'no-store'}).then(r=>{if(!controller.signal.aborted)accept(r);})
      .catch(e=>{if(!controller.signal.aborted)setError(errorText(e));})
      .finally(()=>{if(!controller.signal.aborted)setLoading(false);});
    void loadExecution(controller.signal).catch(e=>{if(!controller.signal.aborted)setExecError(errorText(e));});
    return()=>controller.abort();
  },[path,execPath,executionEnabled,unavailable,refresh]);
  async function refreshExecution(){
    try{await loadExecution();}
    catch(e){setExecError(errorText(e));}
  }
  async function change(action:Action){
    if(pending.current||unavailable||!receipt)return;
    setError('');
    try{
      const payload=goalChange(action,receipt.goal,objective,rounds);
      if(['clear','complete','resume'].includes(action)&&!window.confirm(action==='clear'?'清除当前目标，保留原生历史？':action==='complete'?'确认目标已达到完成条件？':'恢复目标状态？当前不会自动执行。'))return;
      pending.current=true;setBusy(true);
      const result=await post<Receipt>(path,payload);accept(result);
      await refreshExecution();
    }catch(e){
      setError(errorText(e)+' 请刷新状态后再提交，避免重复操作。');
      setReceipt(null);
    }finally{pending.current=false;setBusy(false);}
  }
  async function authorizeExecution(){
    if(pending.current||!executionEnabled||!receipt?.goal||!execution?.harness_session_id)return;
    setExecError('');
    try{
      const payload=goalExecutionAuthorization(receipt.goal,execution.harness_session_id,limits);
      pending.current=true;setBusy(true);
      setExecution(await post<GoalExecution>(execPath,payload));
    }catch(e){setExecError(errorText(e));}
    finally{pending.current=false;setBusy(false);}
  }
  async function startExecution(){
    if(pending.current||!executionEnabled||!receipt?.goal||!execution?.can_start)return;
    setExecError('');
    try{
      pending.current=true;setBusy(true);
      // Confirm the revision on screen. The server rejects a stale confirmation.
      const result=await post<{submitted:boolean;run_id:string}>(execPath+'/rounds',goalRoundStart(receipt.goal));
      if(result.submitted!==true||!result.run_id)throw new Error('未收到本轮执行回执，请刷新状态核对。');
      onRoundStarted?.(result.run_id);
      await refreshExecution();
    }catch(e){setExecError(errorText(e)+' 请刷新状态，避免重复开始。');}
    finally{pending.current=false;setBusy(false);}
  }
  async function stopExecution(){
    if(pending.current||!executionEnabled)return;
    if(!window.confirm('停止后续目标执行？正在运行的一轮不会被取消，但不会再开始下一轮。'))return;
    setExecError('');
    try{
      pending.current=true;setBusy(true);
      setExecution(await api<GoalExecution>(execPath,{method:'DELETE'}));
    }catch(e){setExecError(errorText(e));}
    finally{pending.current=false;setBusy(false);}
  }
  const goal=receipt?.goal;
  const activeAuthorization=execution?.authorization?.status==='active';
  return <Modal title={executionEnabled?'持续目标':'持续目标（状态试点）'} wide onClose={()=>{if(!pending.current)onClose();}}>
    <div className="modal-body">
      <ErrorNotice message={error}/>
      {unavailable&&<p className="section-help">当前会话有任务在运行或已归档；目标状态暂不可修改。目标执行仍可在下方停止。</p>}
      {loading&&<Loader text="正在读取原生目标…"/>}
      {receipt&&<GoalStateView goal={goal||null} executionEnabled={executionEnabled}/>}
      <ErrorNotice message={execError}/>
        {executionEnabled&&execution&&<>
          <GoalExecutionView execution={execution} goal={goal||null}/>
          {!activeAuthorization?<>
            <Field label="本次授权的执行轮次上限" hint="每次开始仍需你单独确认；不会无人值守自动续跑。"><input type="number" min={1} max={32} value={limits.maxRounds} disabled={busy||unavailable||!goal} onChange={e=>setLimits({...limits,maxRounds:Number(e.target.value)})}/></Field>
            <Field label="Token 预算"><input type="number" min={1} value={limits.tokenBudget} disabled={busy||unavailable||!goal} onChange={e=>setLimits({...limits,tokenBudget:Number(e.target.value)})}/></Field>
            <Field label="费用预算（微美元）" hint="1 美元 = 1,000,000 微美元；未知用量不会被当作零。"><input type="number" min={1} value={limits.costBudgetMicros} disabled={busy||unavailable||!goal} onChange={e=>setLimits({...limits,costBudgetMicros:Number(e.target.value)})}/></Field>
            <Field label="单轮费用上限（微美元，可选）" hint="设置后，开始前须为本轮主任务、子任务和重试取得可核验的预留金额。缺少逐笔计量或额度不足时，此轮不会启动。">
              <input type="number" min={1} max={limits.costBudgetMicros} step={1} value={limits.roundCostBudgetMicros??''}
                disabled={busy||unavailable||!goal}
                onChange={e=>setLimits({...limits,roundCostBudgetMicros:e.target.value===''?undefined:Number(e.target.value)})}/>
            </Field>
            <Field label="时间预算（秒）"><input type="number" min={60} value={limits.timeBudgetSeconds} disabled={busy||unavailable||!goal} onChange={e=>setLimits({...limits,timeBudgetSeconds:Number(e.target.value)})}/></Field>
            <div className="task-sidebar-actions">
              <Button busy={busy} disabled={busy||unavailable||!goal||!execution.harness_session_id} onClick={()=>void authorizeExecution()}>授权执行</Button>
            </div>
          </>:<>
            <div className="task-sidebar-actions">
              <Button busy={busy} disabled={busy||unavailable||!goal||!execution.can_start} onClick={()=>void startExecution()}>开始一轮</Button>
              <Button variant="danger" disabled={busy} onClick={()=>void stopExecution()}>停止执行</Button>
            </div>
            {!execution.can_start&&<p className="section-help">当前不能开始新一轮：请检查用量是否完整、预算是否用完、目标是否暂停或已有轮次在运行。</p>}
          </>}
        </>}
        {executionEnabled&&!execution&&!execError&&<p className="section-help">正在读取目标执行状态…</p>}
      {receipt&&<>
        <Field label="目标与完成条件"><textarea rows={6} value={objective} maxLength={8000} disabled={busy||unavailable} onChange={e=>setObjective(e.target.value)}/></Field>
        <Field label="最多目标轮次" hint="此处只保存原生轮次限制，不代表 token、金额或时间预算。"><input type="number" min={1} max={32} value={rounds} disabled={busy||unavailable} onChange={e=>setRounds(Number(e.target.value))}/></Field>
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

export default function ConversationGoal({conversationId,unavailable,executionEnabled=false,onRoundStarted}:{
  conversationId:string;unavailable:boolean;executionEnabled?:boolean;onRoundStarted?:(runId:string)=>void
}){
  const [open,setOpen]=useState(false);
  return <><button className="icon-button" type="button" title={executionEnabled?'持续目标':'持续目标（状态试点）'} aria-label="持续目标" disabled={unavailable&&!executionEnabled} onClick={()=>setOpen(true)}><Target size={17}/></button>
    {open&&<GoalDialog key={conversationId} conversationId={conversationId} unavailable={unavailable} executionEnabled={executionEnabled} onRoundStarted={onRoundStarted} onClose={()=>setOpen(false)}/>}</>;
}
