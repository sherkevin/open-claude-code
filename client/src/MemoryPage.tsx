import MarkdownDocument from './MarkdownDocument';
import SemanticMemoryPanel from './SemanticMemoryPanel';
import { useCallback, useEffect, useRef, useState } from 'react';
import { BookOpen, FilePlus2, FileText, RefreshCw, Save, ShieldCheck, Trash2 } from 'lucide-react';
import { ApiError, errorText, sizeText, timeText } from './api';
import { Button, EmptyState, ErrorNotice, Field, Loader, Modal, PageHeader, SuccessNotice } from './components';
import type { Assistant, Conversation, MemoryFile, MemoryIndex } from './types';
import { deleteMemory, loadMemorySessions, readMemoryFile, readMemoryIndex, writeMemoryFile, memoryMutationKey, memoryMutationMessage, type MemoryMutationKey, type PendingMemoryMutation, readPendingMemoryMutation, persistPendingMemoryMutation, clearPendingMemoryMutation, readMemoryOperation } from './memory';

export default function MemoryPage({assistants,onDirtyChange,onSavingChange}:{assistants:Assistant[];onDirtyChange:(dirty:boolean)=>void;onSavingChange:(saving:boolean)=>void}) {
  const [assistantId,setAssistantId]=useState('');const [selectedId,setSelectedId]=useState('');const [sessions,setSessions]=useState<Conversation[]>([]);const [loading,setLoading]=useState(true);const [error,setError]=useState('');const [reload,setReload]=useState(0);const [workspaceEpoch,setWorkspaceEpoch]=useState(0);
  const [dirty,setDirty]=useState(false);const [saving,setSaving]=useState(false);const [pending,setPending]=useState<(()=>void)|null>(null);const savingRef=useRef(false);
  useEffect(()=>{onDirtyChange(dirty);},[dirty,onDirtyChange]);useEffect(()=>()=>{onDirtyChange(false);onSavingChange(false);},[onDirtyChange,onSavingChange]);
  useEffect(()=>{
    const controller=new AbortController();setLoading(true);setError('');
    loadMemorySessions(assistantId,controller.signal).then(items=>{if(!controller.signal.aborted){setSessions(items);setSelectedId(previous=>items.some(item=>item.id===previous)?previous:items[0]?.id||'');}}).catch(e=>{if(!controller.signal.aborted)setError(errorText(e));}).finally(()=>{if(!controller.signal.aborted)setLoading(false);});
    return()=>controller.abort();
  },[assistantId,reload]);
  const changeSaving=useCallback((value:boolean)=>{savingRef.current=value;setSaving(value);onSavingChange(value);},[onSavingChange]);
  const navigate=(action:()=>void)=>{if(savingRef.current)return;if(dirty)setPending(()=>action);else action();};
  const selected=sessions.find(item=>item.id===selectedId);
  const available=[...assistants.map(item=>({id:item.id,name:item.name})),...sessions.filter(item=>!assistants.some(a=>a.id===item.assistant_id)).filter((item,index,all)=>all.findIndex(other=>other.assistant_id===item.assistant_id)===index).map(item=>({id:item.assistant_id,name:'历史助手'}))];
  if(assistantId&&!available.some(item=>item.id===assistantId))available.push({id:assistantId,name:'历史助手'});
  return <div className="page memory-page"><PageHeader eyebrow="让每项工作独立积累" title="会话记忆" description="先按助手查找会话，再查看和维护这个会话自己的记忆文件。"/>
    <div className="info-strip"><ShieldCheck size={18}/><p>每个会话都有独立记忆。同一助手的其他会话和其他成员都不会共享这里的文件。</p></div>
    <div className="memory-selector memory-session-selector"><Field label="筛选助手"><select value={assistantId} disabled={saving} onChange={event=>{const next=event.target.value;navigate(()=>{setAssistantId(next);setSelectedId('');setSessions([]);setLoading(true);});}}><option value="">全部助手</option>{available.map(item=><option value={item.id} key={item.id}>{item.name}</option>)}</select></Field><Field label="选择会话"><select value={selectedId} disabled={loading||saving||!!error||!sessions.length} onChange={event=>{const next=event.target.value;navigate(()=>setSelectedId(next));}}>{!sessions.length&&<option value="">{loading?'正在读取会话…':'暂无会话'}</option>}{sessions.map(item=><option key={item.id} value={item.id}>{item.title||'新会话'}{item.archived?' · 已归档':''} · {timeText(item.updated_at)}</option>)}</select></Field><Button variant="ghost" disabled={saving||loading} onClick={()=>navigate(()=>setReload(value=>value+1))}><RefreshCw size={15}/>刷新会话</Button></div>
    <p className="memory-session-help">按助手筛选最近会话，包含进行中和已归档会话。页面历史不会因清空记忆而删除；执行时是否继续使用旧上下文，请查看所选会话的记忆边界说明。</p>
    <ErrorNotice message={error}/>{loading?<Loader text="正在读取会话…"/>:error?<Button variant="secondary" onClick={()=>navigate(()=>setReload(value=>value+1))}><RefreshCw size={15}/>重新读取</Button>:selected?<MemoryWorkspace key={`${selected.id}-${workspaceEpoch}`} conversation={selected} onDirtyChange={setDirty} onSavingChange={changeSaving}/>:<EmptyState icon={<BookOpen size={30}/>} title="还没有可管理记忆的会话" description="在工作台新建会话后，它的独立记忆会出现在这里。"/>}
    {pending&&<Modal title="切换前确认未保存的记忆" onClose={()=>setPending(null)}><div className="modal-body"><p>当前会话有未保存草稿或结果尚未确认的操作。切换不会取消已经发送的修改；再次打开此会话时，将恢复查询同一操作编号。请先复制需要保留的草稿，草稿正文不会保存在浏览器缓存中。</p></div><footer className="modal-footer"><Button variant="secondary" onClick={()=>setPending(null)}>继续编辑</Button><Button variant="danger" onClick={()=>{if(savingRef.current)return;const action=pending;setPending(null);setDirty(false);setWorkspaceEpoch(value=>value+1);action();}}>放弃修改并切换</Button></footer></Modal>}
  </div>;
}

export function MemoryWorkspace({conversation,onDirtyChange,onSavingChange}:{conversation:Conversation;onDirtyChange:(dirty:boolean)=>void;onSavingChange:(saving:boolean)=>void}) {
  const [index, setIndex] = useState<MemoryIndex | null>(null); const [loading, setLoading] = useState(true);
  const [document, setDocument] = useState<MemoryFile | null>(null); const [path, setPath] = useState(''); const [content, setContent] = useState(''); const [revision, setRevision] = useState(0);
  const [reading, setReading] = useState(false); const [saving, setSaving] = useState(false); const [error, setError] = useState(''); const [success, setSuccess] = useState('');
  const [newFile, setNewFile] = useState(false); const [newPath, setNewPath] = useState('MEMORY.md');
  const [confirmation, setConfirmation] = useState<{ kind: 'file' | 'all'; path: string; revision: number } | null>(null);
  const sequence = useRef(0); const mounted = useRef(true);const savingRef=useRef(false);const callbacks=useRef({onDirtyChange,onSavingChange});callbacks.current={onDirtyChange,onSavingChange};
  const mutationKey = useRef<MemoryMutationKey|null>(null);
  const harness = conversation.runtime_type === 'harness';
  const [pendingOperation,setPendingOperation]=useState<PendingMemoryMutation|null>(null);
  const [recoveryBlocked,setRecoveryBlocked]=useState(false);
  const dirty = !!path && (!document || content !== document.content);
  useEffect(() => { onDirtyChange(dirty||!!pendingOperation); }, [dirty, pendingOperation, onDirtyChange]);
  const locked = saving || reading || !!index?.busy || recoveryBlocked;
  const bytes = new TextEncoder().encode(content).byteLength;
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; sequence.current += 1;callbacks.current.onDirtyChange(false);callbacks.current.onSavingChange(false); }; }, []);
  const readFile = useCallback(async (nextPath: string) => {
    const requestId = ++sequence.current; setReading(true); setError('');
    try {
      const file = await readMemoryFile(conversation.id,nextPath);
      if (!mounted.current || requestId !== sequence.current) return;
      setDocument(file); setPath(file.path); setContent(file.content); setRevision(file.revision);
    } catch (e) { if (mounted.current && requestId === sequence.current) setError(errorText(e)); }
    finally { if (mounted.current && requestId === sequence.current) setReading(false); }
  }, [conversation.id]);
  const refresh = useCallback(async (selectFile = false) => {
    try {
      const next = await readMemoryIndex(conversation.id);
      if (!mounted.current) return;
      setIndex(next);
      if (selectFile) {
        const first = next.files.find(file => file.path === 'MEMORY.md') || next.files[0];
        if (first) await readFile(first.path);
        else { setDocument(null); setPath(''); setContent(''); setRevision(next.revision); }
      }
    } catch (e) { if (mounted.current) setError(errorText(e)); }
    finally { if (mounted.current) setLoading(false); }
  }, [conversation.id, readFile]);
  const recoverOperation=useCallback(async(operation:PendingMemoryMutation)=>{
    const status=await readMemoryOperation(conversation.id,operation.operation_id);
    if(!mounted.current)return;
    if(status.operation_id!==operation.operation_id)throw new Error('操作编号不匹配，请勿重复修改记忆。');
    if(status.status==='committed'||status.status==='aborted') {
      clearPendingMemoryMutation(conversation.id);setPendingOperation(null);mutationKey.current=null;
      if(status.status==='committed')setSuccess(memoryMutationMessage(operation.kind,status.result||{}));
      else setError('先前的记忆操作未提交，已结束。请读取当前版本后重新编辑。');
      await refresh(status.status==='committed');return true;
    }
    return false;
  },[conversation.id,refresh]);
  useEffect(() => {
    mounted.current=true;
    void (async()=>{
      try {
        let pending:PendingMemoryMutation|null;
        try {pending=harness?readPendingMemoryMutation(conversation.id):null;}catch(e){setRecoveryBlocked(true);throw e;}
        setPendingOperation(pending);
        await refresh(!pending);
        if(pending) {
          if(pending.kind==='save'){setPath(pending.path);setContent('');setDocument(null);setRevision(pending.revision);}
          await recoverOperation(pending);
        }
      } catch(e){if(mounted.current){setError(errorText(e));setLoading(false);}}
    })();
  }, [conversation.id,harness,refresh,recoverOperation]);
  useEffect(() => {
    if (!dirty&&!pendingOperation) return;
    const protect = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', protect); return () => window.removeEventListener('beforeunload', protect);
  }, [dirty,pendingOperation]);
  const mutationError = (e: unknown) => {
    if (e instanceof ApiError && e.code === 'MEMORY_BUSY') { setError('这个会话正在执行任务，暂不能修改它的记忆。任务结束后请刷新再试。'); void refresh(); }
    else if (e instanceof ApiError && e.code === 'MEMORY_REVISION_CONFLICT') { setError('记忆已被其他操作更新，本次未覆盖。请先复制需要保留的草稿，再点击“放弃修改并刷新”读取最新版本。'); }
    else setError(`${errorText(e)}${(!(e instanceof ApiError)||e.status===0||e.status>=500)?' 保存结果可能尚未确认，请保留草稿并重试同一操作；系统会复用操作编号，避免重复修改。':''}`);
  };
  const settleMutationError=async(e:unknown,wasPending:boolean)=>{
    mutationError(e);
    if(!harness)return;
    try {
      const operation=readPendingMemoryMutation(conversation.id);
      if(!operation)return;
      const status=await readMemoryOperation(conversation.id,operation.operation_id);
      if(status.operation_id!==operation.operation_id)return;
      if(status.status==='committed'||status.status==='aborted'){await recoverOperation(operation);return;}
      // A definitive rejection with no earlier uncertain attempt is safe to clear.
      if(status.status==='not_found'&&!wasPending&&e instanceof ApiError&&e.status>=400&&e.status<500){
        clearPendingMemoryMutation(conversation.id);setPendingOperation(null);mutationKey.current=null;
      }
    } catch { /* Keep the operation key when recovery itself is unavailable. */ }
  };
  const save = async () => {
    if (!path || !index || locked || savingRef.current) return;
    savingRef.current=true;setSaving(true);callbacks.current.onSavingChange(true);setError('');setSuccess('');
    try {
      if(pendingOperation&&(pendingOperation.kind!=='save'||pendingOperation.path!==path||pendingOperation.revision!==revision))throw new Error('请先恢复上一次未确认的操作。');
      mutationKey.current=memoryMutationKey(mutationKey.current,{conversation_id:conversation.id,kind:'save',path,content,revision});
      const operation=pendingOperation||{operation_id:mutationKey.current.id,conversation_id:conversation.id,kind:'save' as const,path,revision};
      if(harness){persistPendingMemoryMutation(operation);setPendingOperation(operation);}
      const next = await writeMemoryFile(conversation.id,{path,content,revision},operation.operation_id);
      if (!mounted.current) return;
      if(harness)clearPendingMemoryMutation(conversation.id);setPendingOperation(null);mutationKey.current=null; setDocument(next); setContent(next.content); setRevision(next.revision); setSuccess(memoryMutationMessage('save',next)); await refresh();
    } catch (e) { if (mounted.current) await settleMutationError(e,!!pendingOperation); } finally {savingRef.current=false;if(mounted.current){setSaving(false);callbacks.current.onSavingChange(false);}}
  };
  const remove = async () => {
    if (!confirmation || locked || savingRef.current) return;
    savingRef.current=true;setSaving(true);callbacks.current.onSavingChange(true);setError('');setSuccess('');
    try {
      if(pendingOperation&&(pendingOperation.kind!==confirmation.kind||pendingOperation.path!==confirmation.path||pendingOperation.revision!==confirmation.revision))throw new Error('请先恢复上一次未确认的操作。');
      mutationKey.current=memoryMutationKey(mutationKey.current,{conversation_id:conversation.id,...confirmation});
      const operation=pendingOperation||{operation_id:mutationKey.current.id,conversation_id:conversation.id,...confirmation};
      if(harness){persistPendingMemoryMutation(operation);setPendingOperation(operation);}
      const next=await deleteMemory(conversation.id,confirmation.revision,confirmation.kind==='all'?undefined:confirmation.path,operation.operation_id);
      if (!mounted.current) return;
      setIndex(next); setDocument(null); setPath(''); setContent(''); setRevision(next.revision);
      if(harness)clearPendingMemoryMutation(conversation.id);setPendingOperation(null);mutationKey.current=null; setSuccess(memoryMutationMessage(confirmation.kind,next)); setConfirmation(null);
    } catch (e) { if (mounted.current) { await settleMutationError(e,!!pendingOperation); setConfirmation(null); } } finally {savingRef.current=false;if(mounted.current){setSaving(false);callbacks.current.onSavingChange(false);}}
  };
  if (loading) return <Loader text="正在读取记忆…"/>;
  return <><ErrorNotice message={error} onDismiss={() => setError('')}/><SuccessNotice message={success}/>
    {pendingOperation&&<div className="info-strip"><ShieldCheck size={18}/><div><p>上次操作结果尚未确认，暂不能开始其他修改。系统保留同一操作编号，避免断线重试造成重复修改。{pendingOperation.kind==='save'?'草稿正文不缓存；刷新页面后，请重新填写原正文，再使用原操作编号重试。':''}</p><Button variant="secondary" disabled={saving||reading||recoveryBlocked} onClick={()=>{setError('');void recoverOperation(pendingOperation).catch(e=>setError(errorText(e)));}}>检查上次操作结果</Button>{pendingOperation.kind!=='save'&&<Button variant="secondary" disabled={locked} onClick={()=>setConfirmation({kind:pendingOperation.kind as 'file'|'all',path:pendingOperation.path,revision:pendingOperation.revision})}>重试同一操作</Button>}</div></div>}
    <div className="memory-toolbar"><span className={`badge ${index?.busy ? 'amber' : 'green'}`}>{index?.busy ? '执行中 · 暂时只读' : '可管理记忆'}</span><span>{index?.files.length ?? 0} 个文件 · {sizeText(index?.files.reduce((total, file) => total + file.size, 0) || 0)}</span><div className="toolbar-spacer"/><Button variant="ghost" disabled={saving || reading || dirty || !!pendingOperation} onClick={() => { setError(''); void refresh(true); }}><RefreshCw size={15}/>刷新</Button><Button variant="secondary" disabled={!index || locked || dirty || !!pendingOperation} onClick={() => { setNewPath(index?.files.some(file => file.path === 'MEMORY.md') ? '' : 'MEMORY.md'); setNewFile(true); }}><FilePlus2 size={15}/>新建文件</Button><Button variant="ghost" disabled={!index || (!harness && !index.files.length) || locked || dirty || !!pendingOperation} onClick={() => index && setConfirmation({ kind: 'all', path: '', revision: index.revision })}><Trash2 size={15}/>清空记忆</Button></div>
    <div className="memory-layout"><aside className="memory-files" aria-label="记忆文件"><div className="memory-files-heading"><BookOpen size={16}/><h2>{conversation.title||'新会话'}的记忆</h2></div>{index?.files.length ? index.files.map(file => <button type="button" key={file.path} disabled={saving || reading || dirty || !!pendingOperation} className={`memory-file-item ${file.path === path ? 'selected' : ''}`} onClick={() => { setSuccess(''); void readFile(file.path); }}><FileText size={16}/><span><strong>{file.path}</strong><small>{sizeText(file.size)} · {timeText(file.updated_at)}</small></span></button>) : <p className="section-help memory-no-files">暂无记忆文件。你可以创建 MEMORY.md，也可以在此会话中让助手记住需要持续使用的信息。</p>}</aside>
      <section className="memory-editor">{reading ? <Loader text="正在读取文件…"/> : path ? <><div className="memory-editor-heading"><div><h2>{path}</h2><p>{document ? `更新于 ${timeText(document.updated_at)}` : '新文件 · 尚未保存'}{dirty ? ' · 有未保存修改' : ''}</p></div>{document && <Button variant="ghost" disabled={locked || dirty || !!pendingOperation} onClick={() => setConfirmation({ kind: 'file', path, revision })}><Trash2 size={15}/>删除文件</Button>}</div><div className="field"><span className="field-label">记忆内容</span><MarkdownDocument key={path} text={content} onChange={setContent} label="记忆内容" disabled={locked||!!pendingOperation&&pendingOperation.kind!=='save'}/><small>填写本会话持续使用的偏好、约定或业务背景。不要在这里存放账号密码。</small></div><div className="memory-editor-footer"><span>{sizeText(bytes)} / {sizeText(index?.max_file_bytes || 131072)}</span><div className="memory-editor-actions"><Button variant="secondary" disabled={saving || reading || !!pendingOperation} onClick={() => { setError(''); setSuccess(''); void refresh(true); }}>放弃修改并刷新</Button><Button busy={saving} disabled={!index || locked || bytes > index.max_file_bytes || (!!document && !dirty)} onClick={() => void save()}><Save size={15}/>保存记忆</Button></div></div></> : <EmptyState icon={<BookOpen size={30}/>} title="选择或创建记忆文件" description="MEMORY.md 是这个会话的记忆入口；相关 Markdown 文件可保存更具体的信息。" action={<Button disabled={!index || locked || !!pendingOperation} onClick={() => { setNewPath('MEMORY.md'); setNewFile(true); }}><FilePlus2 size={15}/>创建记忆文件</Button>}/>}</section>
    </div><p className="memory-boundary">{harness ? `文件修改只影响所选会话。有效修改或明确清空后，下一次执行使用新上下文，不会自动重放旧消息、旧记忆与旧附件。页面历史与审计快照仍保留；这不是对模型服务已接收内容的远程删除。${index?.native_epoch!==undefined?` 当前上下文代次：${index.native_epoch}。`:''}` : '文件修改只影响所选会话的记忆。新会话从空白记忆开始；旧版运行时已收到的上下文不会因删除记忆文件而消失。'}</p>
    {harness && index && !conversation.archived && <SemanticMemoryPanel
      key={conversation.id} conversationId={conversation.id} revision={index.revision}
      locked={locked || dirty || !!pendingOperation}/>}
    {newFile && <Modal title="新建记忆文件" description="只接受相对路径的 Markdown 文件，例如 MEMORY.md 或 topics/preferences.md。" onClose={() => setNewFile(false)}><form onSubmit={event => { event.preventDefault(); if (!index) return; const next = newPath.trim(); if (index.files.some(file => file.path === next)) { setError('文件已存在，请在左侧选择并编辑。'); setNewFile(false); return; } setDocument(null); setPath(next); setContent(''); setRevision(index.revision); setNewFile(false); setError(''); }}><div className="modal-body"><Field label="文件路径" required><input value={newPath} onChange={event => setNewPath(event.target.value)} required pattern="[^\\]+\.md" maxLength={240} placeholder="MEMORY.md" autoFocus/></Field></div><footer className="modal-footer"><Button variant="secondary" onClick={() => setNewFile(false)}>取消</Button><Button type="submit" disabled={!newPath.trim()}>开始编辑</Button></footer></form></Modal>}
    {confirmation && <Modal title={confirmation.kind === 'all' ? '清空此会话的记忆' : '删除记忆文件'} onClose={() => { if (!saving) setConfirmation(null); }}><div className="modal-body"><p>{confirmation.kind === 'all' ? `将删除会话「${conversation.title||'新会话'}」的全部记忆文件。` : `将删除「${confirmation.path}」。`}{harness?'当前记忆入口将更新，已发布的历史快照仍保留用于审计；请先保留仍需继续使用的内容。':'此操作无法撤销，请先保留仍需要的内容。'}</p><p className="section-help">{harness?'其他会话不受影响。即使当前没有记忆文件，确认清空也会切换上下文代次；下一次执行不再自动使用旧消息、旧记忆和旧附件，页面历史不会删除。':'其他会话的记忆不受影响。这个会话的消息、附件与已有上下文不会删除。'}</p></div><footer className="modal-footer"><Button variant="secondary" disabled={saving} onClick={() => setConfirmation(null)}>保留记忆</Button><Button variant="danger" busy={saving} disabled={!!index?.busy} onClick={() => void remove()}>{confirmation.kind === 'all' ? '确认清空' : '确认删除'}</Button></footer></Modal>}
  </>;
}
