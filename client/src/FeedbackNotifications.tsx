import {useEffect,useState,useCallback} from 'react';
import {Bell,RefreshCw} from 'lucide-react';
import {api,errorText,patch,timeText} from './api';
import {Button,EmptyState,ErrorNotice,Loader,PageHeader} from './components';
import type {Conversation} from './types';

export type FeedbackNotice={id:string;conversation_id:string;message_id?:string|null;status:'new'|'in_progress'|'resolved';description:string;resolution_note:string;updated_at:string;version:number};
type NoticeData={unread_count:number;items:FeedbackNotice[]};
type InboxState={owner?:string;data?:NoticeData;loading:boolean;error:string};
const initial:InboxState={loading:false,error:''};
const labels={new:'待处理',in_progress:'处理中',resolved:'已解决'} as const;

export function useFeedbackNotifications(owner?:string){
  const [state,setState]=useState<InboxState>(initial);
  const [revision,setRevision]=useState(0);
  const refresh=useCallback(()=>setRevision(value=>value+1),[]);
  useEffect(()=>{
    if(!owner)return;
    let active=true;
    let request:AbortController|undefined;
    async function load(){
      if(!active)return;
      request?.abort();
      const controller=new AbortController();
      request=controller;
      setState(old=>({owner,data:old.owner===owner?old.data:undefined,loading:true,error:''}));
      try{
        const data=await api<NoticeData>('/feedback/notifications',{signal:controller.signal,cache:'no-store'});
        if(active&&!controller.signal.aborted)setState({owner,data,loading:false,error:''});
      }catch(e){
        if(active&&!controller.signal.aborted)setState(old=>({owner,data:old.owner===owner?old.data:undefined,loading:false,error:errorText(e)}));
      }
    }
    function refreshVisible(){if(document.visibilityState==='visible')void load();}
    void load();
    const interval=window.setInterval(refreshVisible,60000);
    window.addEventListener('focus',refreshVisible);
    document.addEventListener('visibilitychange',refreshVisible);
    return ()=>{active=false;request?.abort();window.clearInterval(interval);window.removeEventListener('focus',refreshVisible);document.removeEventListener('visibilitychange',refreshVisible);};
  },[owner,revision]);
  return {...(owner&&state.owner===owner?state:initial),refresh};
}

export default function FeedbackNotifications({inbox,onOpen}:{inbox:ReturnType<typeof useFeedbackNotifications>;onOpen:(conversation:Conversation,messageId?:string)=>void}){
  const [busy,setBusy]=useState(''),[error,setError]=useState('');
  async function read(item:FeedbackNotice){
    if(busy)return;
    setBusy(item.id);setError('');
    try{await patch('/feedback/notifications/'+encodeURIComponent(item.id),{expected_version:item.version});}
    catch(e){setError(errorText(e));}
    finally{inbox.refresh();setBusy('');}
  }
  async function open(item:FeedbackNotice){
    if(busy)return;
    setBusy(item.id);setError('');
    try{const conversation=await api<Conversation>('/conversations/'+encodeURIComponent(item.conversation_id));onOpen(conversation,item.message_id||undefined);}
    catch(e){setError(errorText(e));}
    finally{setBusy('');}
  }
  const rows=inbox.data?.items||[];
  return <div className="page"><PageHeader title="反馈通知" eyebrow="我的工作台" description="查看管理员更新的处理结果。打开会话不会自动标记已读。" actions={<Button variant="secondary" busy={inbox.loading} onClick={inbox.refresh}><RefreshCw size={16}/>刷新</Button>}/>
    <ErrorNotice message={inbox.error}/><ErrorNotice message={error}/>
    {inbox.data&&<p role="status">未读反馈：{inbox.data.unread_count}{inbox.data.unread_count>rows.length?'（先显示最近 100 条）':''}</p>}
    {!inbox.data&&!inbox.error?<Loader/>:!rows.length&&!inbox.error?<EmptyState icon={<Bell size={30}/>} title="暂无未读反馈" description="管理员更新反馈处理结果后，你将在此收到站内通知。已读结果仍可在原会话的问题反馈中查询。"/>:rows.map(item=><article key={item.id} className="approval-card" aria-label="反馈通知">
      <h3>{labels[item.status]||item.status}</h3><p className="form-note">{timeText(item.updated_at)}</p>
      <p style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}}>{item.description}</p>
      {item.resolution_note&&<p style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}}><strong>管理员处理说明：</strong>{item.resolution_note}</p>}
      <p className="form-note">反馈编号：<code>{item.id}</code></p>
      <div className="checkbox-row"><Button variant="secondary" disabled={!!busy} onClick={()=>void open(item)}>打开关联会话</Button><Button disabled={!!busy||inbox.loading} busy={busy===item.id} onClick={()=>void read(item)}>标为已读</Button></div>
    </article>)}
  </div>;
}
