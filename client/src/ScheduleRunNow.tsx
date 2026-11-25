import {useRef,useState} from 'react';
import {clientRequestId,errorText,post,timeText} from './api';
import {Button,ErrorNotice,Modal} from './components';
import type {Schedule} from './ConversationTasks';

export type RunNowReceipt={plan_id:string;occurrence_id:string;created_at:string;status:string;run_id?:string|null;message_id?:string|null};
export function runNowPayload(task:Schedule,requestId:string){
  if(!task.edit_token||task.status!=='scheduled'||task.series_id)throw new Error('计划状态已变化，请刷新后重试。');
  return {request_id:requestId,expected_token:task.edit_token};
}
const labels:Record<string,string>={pending:'已进入待发队列',queued:'已排队',running:'执行中',awaiting_approval:'等待审批',accepted:'已提交运行',succeeded:'已完成',failed:'执行失败',cancelled:'已取消',interrupted:'已中断',cancelling:'正在取消'};

export default function ScheduleRunNow({task,path,onClose,onQueued}:{task:Schedule;path:string;onClose:()=>void;onQueued:()=>void}){
  const [requestId]=useState(clientRequestId);
  const [busy,setBusy]=useState(false),[error,setError]=useState(''),[receipt,setReceipt]=useState<RunNowReceipt|null>(null);
  const pending=useRef(false);
  const series=!!task.recurrence&&task.recurrence!=='once';
  async function run(){
    if(pending.current||receipt)return;
    pending.current=true;setBusy(true);setError('');
    try{
      const result=await post<RunNowReceipt>(path+'/'+encodeURIComponent(task.id)+'/run-now',runNowPayload(task,requestId));
      if(result.plan_id!==task.id||!result.occurrence_id)throw new Error('未取得匹配的执行回执，请查询原计划状态。');
      setReceipt(result);onQueued();
    }catch(e){setError(errorText(e));}
    finally{pending.current=false;setBusy(false);}
  }
  return <Modal title="立即执行定时任务" description={series?'额外执行一次，原周期时间保持不变。':'提前执行该单次任务，原时间不会再次触发。'} onClose={()=>{if(!pending.current)onClose();}} wide>
    <div className="modal-body">
      <ErrorNotice message={error}/>
      {receipt?<div role="status"><strong>{labels[receipt.status]||'已记录执行请求'}</strong><p>执行实例编号：{receipt.occurrence_id}</p><p>请求时间：{timeText(receipt.created_at)}</p><p>请在定时任务中心或关联会话查看实际执行状态和结果；提交回执不代表业务执行成功。</p></div>:<>
        <p className="section-help">原计划时间：{timeText(task.scheduled_at)}。任务仍按会话队列顺序执行，并遵守现有工具权限和审批策略；若上次运行存在未确认结果或周期即将到期，系统会拒绝。</p>
        <p style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}}>{task.text}</p>
        {error&&<p className="section-help">同一窗口重试使用原请求编号，不会重复提交。关闭后请先查看任务历史，确认是否已执行。</p>}
      </>}
    </div>
    <footer className="modal-footer">
      <Button variant="secondary" disabled={busy} onClick={onClose}>{receipt?'关闭':'取消'}</Button>
      {!receipt&&<Button busy={busy} disabled={busy} onClick={()=>void run()}>{error?'重试同一请求':'确认立即执行'}</Button>}
    </footer>
  </Modal>;
}
