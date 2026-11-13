import { useEffect, useRef, useState } from 'react';
import { Check, ShieldCheck, X } from 'lucide-react';
import { api, errorText, sizeText, timeText } from './api';
import { Button, ErrorNotice, Loader } from './components';
import type { Approval, MemoryApprovalContent } from './types';

export const MEMORY_WRITE_TOOL = 'mcp__workspace__memory_write';
export function validateMemoryReview(value: MemoryApprovalContent,conversationId?:string): MemoryApprovalContent {
  if (!value || typeof value.content !== 'string' || new TextEncoder().encode(value.content).length > 131072
    || typeof value.path !== 'string' || !value.path || typeof value.operation_id !== 'string' || !value.operation_id
    || !Number.isSafeInteger(value.expected_revision) || value.expected_revision < 0
    || !Number.isSafeInteger(value.native_epoch) || value.native_epoch < 0
    || typeof value.conversation_id!=='string'||!value.conversation_id||(conversationId!==undefined&&value.conversation_id!==conversationId)
    || (value.old_content_sha256!==null&&!/^[0-9a-f]{64}$/.test(value.old_content_sha256))
    || !/^[0-9a-f]{64}$/.test(value.content_digest)) throw new Error('记忆审批正文不完整，请重新读取。');
  return value;
}

export function MemoryReviewBody({ value }: { value: MemoryApprovalContent }) {
  return <><p>将写入或完整替换 <strong>{value.path}</strong> · 版本 {value.expected_revision} · 上下文代次 {value.native_epoch} · {sizeText(new TextEncoder().encode(value.content).length)}</p>
    <p className="section-help" style={{overflowWrap:'anywhere'}}>仅当前会话：{value.conversation_id}。{value.old_content_sha256?`替换前文件摘要（SHA-256）：${value.old_content_sha256}`:'这是新文件，不会覆盖现有文件。'}</p>
    <pre data-memory-review="full-content" tabIndex={0} aria-label="待写入的完整记忆正文" style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere',maxHeight:'24rem',overflow:'auto'}}>{value.content || '（空文件）'}</pre>
    <p className="section-help">批准仅将内容暂存。只有本轮任务成功、执行进程结束且授权仍有效时，才会正式保存；失败或取消不会写入。有效保存后，下一轮不会自动带入旧上下文。</p></>;
}

export default function MemoryApprovalCard({approval,conversationId,busy,onDecision}:{approval:Approval;conversationId:string;busy:boolean;onDecision:(decision:'allow'|'deny',digest?:string)=>Promise<boolean>}) {
  const [value,setValue]=useState<MemoryApprovalContent|null>(null);
  const [loading,setLoading]=useState(true);const [error,setError]=useState('');const [confirmed,setConfirmed]=useState(false);const [attempt,setAttempt]=useState(0);
  const deadline=new Date(approval.expires_at).getTime();
  const [expired,setExpired]=useState(!Number.isFinite(deadline)||deadline<=Date.now());
  const submitting=useRef(false);
  const denied=approval.approval_policy==='deny_by_default'||approval.approval_mode==='dual'||approval.strategy==='dual';
  useEffect(()=>{
    const remaining=deadline-Date.now();
    if (!Number.isFinite(remaining)||remaining<=0) { setExpired(true);return; }
    setExpired(false);
    const timer=setTimeout(()=>{setExpired(true);setValue(null);setConfirmed(false);},Math.min(remaining,2147483647));
    return()=>clearTimeout(timer);
  },[deadline]);
  useEffect(()=>{
    const controller=new AbortController();setValue(null);setConfirmed(false);setError('');setLoading(true);
    if(expired||denied){setLoading(false);return()=>controller.abort();}
    api<MemoryApprovalContent>(`/approvals/${encodeURIComponent(approval.id)}/memory-content`,{signal:controller.signal})
      .then(result=>{if(!controller.signal.aborted)setValue(validateMemoryReview(result,conversationId));})
      .catch(e=>{if(!controller.signal.aborted){setValue(null);setError(errorText(e));}})
      .finally(()=>{if(!controller.signal.aborted)setLoading(false);});
    return()=>controller.abort();
  },[approval.id,conversationId,attempt,expired,denied]);
  const decide=async(decision:'allow'|'deny')=>{
    if(submitting.current||busy||expired||Date.now()>=deadline)return;
    if(decision==='allow'&&(!value||!confirmed||denied))return;
    submitting.current=true;
    try {
      const accepted=await onDecision(decision,decision==='allow'?value!.content_digest:undefined);
      if(accepted){setValue(null);setConfirmed(false);}
      else setError('审批请求未确认，可保留当前正文并重试；系统不会绕过服务端重新校验。');
    } finally { submitting.current=false; }
  };
  return <section className="approval-card"><div className="approval-title"><ShieldCheck size={18}/><strong>确认会话记忆写入</strong></div>
    <p className="approval-impact">仅会话本人可查看完整正文并确认；不允许模型代审。</p>
    {loading?<Loader text="正在校验权限并读取完整正文…"/>:value?<MemoryReviewBody value={value}/>:null}
    <ErrorNotice message={error}/>{error&&!expired&&!denied&&<Button variant="secondary" disabled={busy||loading} onClick={()=>setAttempt(n=>n+1)}>重新读取完整正文</Button>}
    <p>{denied?'当前审批策略不允许此私有记忆写入。':expired?'此记忆审批已过期。':`有效至 ${timeText(approval.expires_at)}`}</p>
    {value&&!expired&&<label><input type="checkbox" checked={confirmed} disabled={busy} onChange={event=>setConfirmed(event.target.checked)}/>我已核对完整正文，允许将这份内容暂存，等待任务成功后保存。</label>}
    <div className="approval-actions"><Button variant="secondary" disabled={busy||expired} onClick={()=>void decide('deny')}><X size={15}/>拒绝</Button><Button busy={busy} disabled={expired||denied||!value||!confirmed||loading} onClick={()=>void decide('allow')}><Check size={15}/>批准暂存</Button></div>
  </section>;
}
