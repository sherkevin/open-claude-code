import {useCallback,useEffect,useState} from 'react';
import {MessageSquareWarning,RefreshCw} from 'lucide-react';
import {api,apiURL,errorText,patch,timeText} from './api';
import {Button,EmptyState,ErrorNotice,Loader,Modal,PageHeader} from './components';

type Row={id:string;user_id:string;display_name:string;conversation_id:string;run_id?:string;message_id?:string;status:string;created_at:string;image_count:number};
type Detail=Row&{description:string;updated_at:string;resolution_note?:string;images:{id:string;mime:string;size:number}[];diagnostics?:{run:Record<string,unknown>;events:{id:number;type:string;created_at:string;data:Record<string,unknown>}[]}};
const labels:Record<string,string>={new:'待处理',in_progress:'处理中',resolved:'已解决'};
export default function FeedbackPage(){
  const [rows,setRows]=useState<Row[]>([]),[detail,setDetail]=useState<Detail|null>(null),[loading,setLoading]=useState(true),[busy,setBusy]=useState(false),[error,setError]=useState(''),[filter,setFilter]=useState('all');
  const [draftStatus,setDraftStatus]=useState('new'),[draftNote,setDraftNote]=useState('');
  const load=useCallback(async()=>{setLoading(true);setError('');try{setRows(await api<Row[]>('/admin/feedback'));}catch(e){setError(errorText(e));}finally{setLoading(false);}},[]);
  useEffect(()=>{void load();},[load]);
  async function open(id:string){setBusy(true);setError('');try{const item=await api<Detail>('/admin/feedback/'+encodeURIComponent(id));setDetail(item);setDraftStatus(item.status);setDraftNote(item.resolution_note||'');}catch(e){setError(errorText(e));}finally{setBusy(false);}}
  async function publish(){if(!detail||busy)return;
    if(!window.confirm('处理状态和说明将对反馈提交者可见。请确认不包含内部日志、凭据或其他用户信息。'))return;
    setBusy(true);setError('');
    try{const result=await patch<{status:string;resolution_note:string;updated_at:string}>('/admin/feedback/'+encodeURIComponent(detail.id),{status:draftStatus,resolution_note:draftNote,expected_updated_at:detail.updated_at});setDetail({...detail,...result});setDraftStatus(result.status);setDraftNote(result.resolution_note);setRows(old=>old.map(row=>row.id===detail.id?{...row,status:result.status}:row));}
    catch(e){setError(errorText(e));}finally{setBusy(false);}
  }
  const visible=rows.filter(row=>filter==='all'||row.status===filter);
  return <div className="page"><PageHeader title="问题反馈管理" eyebrow="产品改进" description="查看用户主动提交的反馈、截图和关联执行日志。" actions={<Button variant="secondary" busy={loading} onClick={()=>void load()}><RefreshCw size={16}/>刷新</Button>}/>
    <ErrorNotice message={error}/><label>处理状态 <select value={filter} onChange={e=>setFilter(e.target.value)}><option value="all">全部</option>{Object.entries(labels).map(([id,label])=><option key={id} value={id}>{label}</option>)}</select></label>
    {loading?<Loader/>:!visible.length?<EmptyState icon={<MessageSquareWarning size={30}/>} title="暂无相关反馈" description="使用者可以在会话右上角提交问题。"/>:<div className="table-container"><table><thead><tr><th>反馈时间</th><th>提交者</th><th>状态</th><th>截图</th><th>关联轮次</th><th>操作</th></tr></thead><tbody>{visible.map(row=><tr key={row.id}><td>{timeText(row.created_at)}</td><td>{row.display_name}</td><td>{labels[row.status]||row.status}</td><td>{row.image_count}</td><td>{row.run_id?'已关联':'会话级'}</td><td><Button variant="ghost" disabled={busy} onClick={()=>void open(row.id)}>查看详情</Button></td></tr>)}</tbody></table></div>}
    {detail&&<Modal title="反馈详情" wide onClose={()=>{if(!busy)setDetail(null);}}><div className="modal-body"><ErrorNotice message={error}/><dl><dt>反馈编号</dt><dd><code>{detail.id}</code></dd><dt>会话 ID</dt><dd><code>{detail.conversation_id}</code></dd><dt>执行轮次 ID</dt><dd><code>{detail.run_id||'未指定'}</code></dd><dt>消息 ID</dt><dd><code>{detail.message_id||'未指定'}</code></dd></dl><h3>问题描述</h3><p style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}}>{detail.description}</p>
      {detail.images.map(img=><a key={img.id} href={apiURL('/admin/feedback/'+encodeURIComponent(detail.id)+'/images/'+encodeURIComponent(img.id))} target="_blank" rel="noreferrer"><img alt="用户上传的反馈截图" loading="lazy" style={{maxWidth:'100%',maxHeight:360,objectFit:'contain'}} src={apiURL('/admin/feedback/'+encodeURIComponent(detail.id)+'/images/'+encodeURIComponent(img.id))}/></a>)}
      <h3>关联执行状态</h3>{detail.diagnostics?<><pre style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}}>{JSON.stringify(detail.diagnostics.run,null,2)}</pre><div className="table-container"><table><thead><tr><th>时间</th><th>事件</th><th>诊断字段</th></tr></thead><tbody>{detail.diagnostics.events.map(e=><tr key={e.id}><td>{timeText(e.created_at)}</td><td>{e.type}</td><td><pre style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}}>{JSON.stringify(e.data)}</pre></td></tr>)}</tbody></table></div></>:<p>没有指定执行轮次；可按会话编号排查。</p>}
      <label>处理状态 <select disabled={busy} value={draftStatus} onChange={e=>setDraftStatus(e.target.value)}>{Object.entries(labels).map(([id,label])=><option key={id} value={id}>{label}</option>)}</select></label>
      <label>处理说明（提交者可见）<textarea value={draftNote} onChange={e=>setDraftNote(e.target.value)} maxLength={2000} rows={4} disabled={busy} placeholder="说明解决结果或后续处理安排"/></label>
      <p className="form-note">仅填写适合提交者阅读的说明，不要包含内部诊断日志、凭据或其他用户信息。清空说明并发布会撤回旧说明。</p>
    </div><footer className="modal-footer"><Button variant="secondary" disabled={busy} onClick={()=>setDetail(null)}>关闭</Button><Button busy={busy} disabled={draftStatus===detail.status&&draftNote.trim()===(detail.resolution_note||'')} onClick={()=>void publish()}>发布处理结果</Button></footer></Modal>}
  </div>;
}
