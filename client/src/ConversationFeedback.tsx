import {useRef,useState} from 'react';
import {MessageSquareWarning} from 'lucide-react';
import {api,clientRequestId,errorText,timeText,sizeText} from './api';
import {Button,ErrorNotice,Field,Modal} from './components';
import type {Message} from './types';

const MAX_FILE=4*1024*1024, MAX_TOTAL=8*1024*1024;
export function feedbackImageProblem(files: File[]) {
  if(files.length>3) return '最多上传 3 张截图。';
  if(files.some(file=>!['image/png','image/jpeg','image/webp'].includes(file.type)||file.size>MAX_FILE||!file.size)) return '仅支持 PNG、JPG、WebP，每张不超过 4 MB。';
  if(files.reduce((total,file)=>total+file.size,0)>MAX_TOTAL) return '截图合计不能超过 8 MB。';
  return '';
}
export function feedbackForm(description:string,requestId:string,target:string,runId:string|undefined,files:File[]){
  const body=new FormData();
  body.append('description',description.trim());body.append('request_id',requestId);
  if(target==='current'&&runId)body.append('run_id',runId);
  else if(target.startsWith('message:'))body.append('message_id',target.slice(8));
  files.forEach(file=>body.append('screenshots',file,file.name));
  return body;
}
export default function ConversationFeedback({conversationId,runId,messages}:{conversationId:string;runId?:string;messages:Message[]}){
  const [open,setOpen]=useState(false),[description,setDescription]=useState(''),[target,setTarget]=useState('current'),[files,setFiles]=useState<File[]>([]),[busy,setBusy]=useState(false),[error,setError]=useState(''),[receipt,setReceipt]=useState('');
  const pending=useRef(false),requestId=useRef(''),frozenRun=useRef<string>();
  function start(){requestId.current=clientRequestId();frozenRun.current=runId;setTarget(runId?'current':'session');setDescription('');setFiles([]);setReceipt('');setError('');setOpen(true);}
  async function submit(){
    if(pending.current||!description.trim())return;
    const issue=feedbackImageProblem(files);if(issue){setError(issue);return;}
    pending.current=true;setBusy(true);setError('');
    try{const result=await api<{id:string}>('/conversations/'+encodeURIComponent(conversationId)+'/feedback',{method:'POST',body:feedbackForm(description,requestId.current,target,frozenRun.current,files)});setReceipt(result.id);setFiles([]);}
    catch(e){setError(errorText(e));}finally{pending.current=false;setBusy(false);}
  }
  return <><button type="button" className="icon-button" title="问题反馈" aria-label="问题反馈" onClick={start}><MessageSquareWarning size={17}/></button>
  {open&&<Modal title="问题反馈" description="帮助我们定位和修复本次使用中的问题。" onClose={()=>{if(!pending.current)setOpen(false);}} wide>
    <div className="modal-body"><ErrorNotice message={error}/>{receipt?<div role="status"><strong>反馈已提交</strong><p>反馈编号：{receipt}</p><p>工作台管理员可在反馈管理中查看和处理。</p></div>:<>
      <Field label="问题发生位置"><select value={target} disabled={busy} onChange={e=>setTarget(e.target.value)}>
        <option value="session">整个会话</option>{frozenRun.current&&<option value="current">打开反馈时的当前执行轮次</option>}
        {messages.slice(-40).reverse().map(m=><option key={m.id} value={'message:'+m.id}>{timeText(m.created_at)} · {m.role==='user'?'提问':'回答'} · {m.text.trim().replace(/\s+/g,' ').slice(0,60)||'附件消息'}</option>)}
      </select></Field>
      <Field label="问题描述" required><textarea value={description} disabled={busy} onChange={e=>setDescription(e.target.value)} rows={5} maxLength={4000} placeholder="发生了什么？预期结果是什么？如何复现？"/></Field>
      <Field label="上传截图（可选）" hint="最多 3 张，每张 4 MB，合计 8 MB。"><input type="file" multiple accept="image/png,image/jpeg,image/webp" disabled={busy} onChange={e=>{const next=[...files,...Array.from(e.target.files||[])];e.target.value='';const issue=feedbackImageProblem(next);setError(issue);if(!issue)setFiles(next);}}/></Field>
      {files.map((file,i)=><div key={i} className="checkbox-row"><span>{file.name} · {sizeText(file.size)}</span><Button variant="ghost" disabled={busy} onClick={()=>setFiles(old=>old.filter((_,index)=>index!==i))}>移除</Button></div>)}
      <p className="form-note">提交后，工作台管理员可以查看你填写的内容、所选截图、关联会话/轮次编号和有限的执行状态日志。不会自动附上完整对话、请求参数或业务结果。请遮盖截图中的敏感信息；反馈默认保留 90 天。</p>
    </>}</div><footer className="modal-footer"><Button variant="secondary" disabled={busy} onClick={()=>setOpen(false)}>{receipt?'关闭':'取消'}</Button>{!receipt&&<Button busy={busy} disabled={busy||!description.trim()} onClick={()=>void submit()}>提交反馈</Button>}</footer>
  </Modal>}</>;
}
