import {useRef,useState} from 'react';
import {errorText,patch,timeText} from './api';
import {Button,ErrorNotice,Field,Modal} from './components';
import type {Schedule} from './ConversationTasks';

export function localDateTime(iso:string){
  const d=new Date(iso);
  if(!Number.isFinite(d.getTime()))return '';
  const pad=(n:number)=>String(n).padStart(2,'0');
  return d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate())+'T'+pad(d.getHours())+':'+pad(d.getMinutes());
}

export function scheduleEditPayload(task:Schedule,text:string,changeTiming:boolean,when:string,rule:string,zone:string){
  if(!task.edit_token)throw new Error('计划版本缺失，请刷新后重试。');
  if(!text.trim())throw new Error('请输入任务内容。');
  const body:{text:string;expected_token:string;scheduled_at?:string;rule_expression?:string;timezone?:string}={text:text.trim(),expected_token:task.edit_token};
  if(changeTiming){
    if(task.recurrence==='cron'||task.recurrence==='rrule'){
      if(!rule.trim()||!zone.trim())throw new Error('请输入完整规则和时区。');
      body.rule_expression=rule.trim();body.timezone=zone.trim();
    }else{
      const date=new Date(when);
      if(!Number.isFinite(date.getTime())||date.getTime()<=Date.now())throw new Error('请选择未来的执行时间。');
      body.scheduled_at=date.toISOString();
    }
  }
  return body;
}

export default function ScheduleEditor({task,path,onClose,onSaved}:{task:Schedule;path:string;onClose:()=>void;onSaved:()=>void}){
  const [text,setText]=useState(task.text),[changeTiming,setChangeTiming]=useState(false),[when,setWhen]=useState(()=>localDateTime(task.scheduled_at)),[rule,setRule]=useState(task.rule_expression||''),[zone,setZone]=useState(task.timezone||Intl.DateTimeFormat().resolvedOptions().timeZone||'UTC'),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const pending=useRef(false);
  const advanced=task.recurrence==='cron'||task.recurrence==='rrule';
  async function save(){
    if(pending.current)return;
    setError('');
    try{
      const payload=scheduleEditPayload(task,text,changeTiming,when,rule,zone);
      pending.current=true;setBusy(true);
      await patch(path+'/'+encodeURIComponent(task.id),payload);
      onSaved();
    }catch(e){setError(errorText(e));}
    finally{pending.current=false;setBusy(false);}
  }
  return <Modal title="编辑定时任务" description="只修改尚未开始的计划，不改动历史执行或已提交的业务操作。" onClose={()=>{if(!pending.current)onClose();}} wide>
    <div className="modal-body">
      <ErrorNotice message={error}/>
      <p className="section-help">当前执行时间：{timeText(task.scheduled_at)}。执行模式及频率类别保持不变。{task.status==='paused'?'编辑不会恢复暂停状态。':''}</p>
      <Field label="任务内容" required><textarea value={text} disabled={busy} onChange={e=>setText(e.target.value)} rows={5} maxLength={50000}/></Field>
      <label className="checkbox-row"><input type="checkbox" checked={changeTiming} disabled={busy} onChange={e=>setChangeTiming(e.target.checked)}/>修改执行时间或周期规则</label>
      {changeTiming&&(advanced?<><Field label="时区"><input value={zone} disabled={busy} onChange={e=>setZone(e.target.value)} maxLength={100}/></Field>
        <Field label="完整周期规则"><textarea value={rule} disabled={busy} onChange={e=>setRule(e.target.value)} rows={4} maxLength={2048}/></Field>
        <p className="section-help">由官方 Harness 重新计算未来时间；修改有限次数 RRULE 会以新规则重新计算剩余执行次数。</p></>:
        <Field label="新的执行时间（浏览器本地时间）" hint={task.timezone?'以后周期仍按原时区 '+task.timezone+' 计算。':undefined}><input type="datetime-local" value={when} disabled={busy} onChange={e=>setWhen(e.target.value)}/></Field>)}
    </div>
    <footer className="modal-footer"><Button variant="secondary" disabled={busy} onClick={onClose}>取消</Button><Button busy={busy} disabled={busy||!text.trim()} onClick={()=>void save()}>保存修改</Button></footer>
  </Modal>;
}
