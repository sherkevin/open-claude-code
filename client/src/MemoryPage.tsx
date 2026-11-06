import { useCallback, useEffect, useRef, useState } from 'react';
import { BookOpen, FilePlus2, FileText, RefreshCw, Save, ShieldCheck, Trash2 } from 'lucide-react';
import { ApiError, errorText, sizeText, timeText } from './api';
import { Button, EmptyState, ErrorNotice, Field, Loader, Modal, PageHeader, SuccessNotice } from './components';
import type { Assistant, Conversation, MemoryFile, MemoryIndex } from './types';
import { deleteMemory, loadMemorySessions, readMemoryFile, readMemoryIndex, writeMemoryFile } from './memory';

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
    <p className="memory-session-help">按助手筛选最近会话，包含进行中和已归档会话。清空记忆不会删除这个会话已有的消息与上下文。</p>
    <ErrorNotice message={error}/>{loading?<Loader text="正在读取会话…"/>:error?<Button variant="secondary" onClick={()=>navigate(()=>setReload(value=>value+1))}><RefreshCw size={15}/>重新读取</Button>:selected?<MemoryWorkspace key={`${selected.id}-${workspaceEpoch}`} conversation={selected} onDirtyChange={setDirty} onSavingChange={changeSaving}/>:<EmptyState icon={<BookOpen size={30}/>} title="还没有可管理记忆的会话" description="在工作台新建会话后，它的独立记忆会出现在这里。"/>}
    {pending&&<Modal title="切换前确认未保存的记忆" onClose={()=>setPending(null)}><div className="modal-body"><p>当前会话的记忆有未保存修改。你可以继续编辑并保存，或放弃草稿后继续。</p></div><footer className="modal-footer"><Button variant="secondary" onClick={()=>setPending(null)}>继续编辑</Button><Button variant="danger" onClick={()=>{if(savingRef.current)return;const action=pending;setPending(null);setDirty(false);setWorkspaceEpoch(value=>value+1);action();}}>放弃修改并切换</Button></footer></Modal>}
  </div>;
}

function MemoryWorkspace({conversation,onDirtyChange,onSavingChange}:{conversation:Conversation;onDirtyChange:(dirty:boolean)=>void;onSavingChange:(saving:boolean)=>void}) {
  const [index, setIndex] = useState<MemoryIndex | null>(null); const [loading, setLoading] = useState(true);
  const [document, setDocument] = useState<MemoryFile | null>(null); const [path, setPath] = useState(''); const [content, setContent] = useState(''); const [revision, setRevision] = useState(0);
  const [reading, setReading] = useState(false); const [saving, setSaving] = useState(false); const [error, setError] = useState(''); const [success, setSuccess] = useState('');
  const [newFile, setNewFile] = useState(false); const [newPath, setNewPath] = useState('MEMORY.md');
  const [confirmation, setConfirmation] = useState<{ kind: 'file' | 'all'; path: string; revision: number } | null>(null);
  const sequence = useRef(0); const mounted = useRef(true);const savingRef=useRef(false);const callbacks=useRef({onDirtyChange,onSavingChange});callbacks.current={onDirtyChange,onSavingChange};
  const dirty = !!path && (!document || content !== document.content);
  useEffect(() => { onDirtyChange(dirty); }, [dirty, onDirtyChange]);
  const locked = saving || reading || !!index?.busy;
  const bytes = new TextEncoder().encode(content).byteLength;
  useEffect(() => () => { mounted.current = false; sequence.current += 1;callbacks.current.onDirtyChange(false);callbacks.current.onSavingChange(false); }, []);
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
  useEffect(() => { mounted.current = true; void refresh(true); }, [refresh]);
  useEffect(() => {
    if (!dirty) return;
    const protect = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', protect); return () => window.removeEventListener('beforeunload', protect);
  }, [dirty]);
  const mutationError = (e: unknown) => {
    if (e instanceof ApiError && e.code === 'MEMORY_BUSY') { setError('这个会话正在执行任务，暂不能修改它的记忆。任务结束后请刷新再试。'); void refresh(); }
    else if (e instanceof ApiError && e.code === 'MEMORY_REVISION_CONFLICT') { setError('记忆已被其他操作更新，本次未覆盖。请先复制需要保留的草稿，再点击“放弃修改并刷新”读取最新版本。'); }
    else setError(errorText(e));
  };
  const save = async () => {
    if (!path || !index || locked || savingRef.current) return;
    savingRef.current=true;setSaving(true);callbacks.current.onSavingChange(true);setError('');setSuccess('');
    try {
      const next = await writeMemoryFile(conversation.id,{path,content,revision});
      if (!mounted.current) return;
      setDocument(next); setContent(next.content); setRevision(next.revision); setSuccess('记忆文件已保存。'); await refresh();
    } catch (e) { if (mounted.current) mutationError(e); } finally {savingRef.current=false;if(mounted.current){setSaving(false);callbacks.current.onSavingChange(false);}}
  };
  const remove = async () => {
    if (!confirmation || locked || savingRef.current) return;
    savingRef.current=true;setSaving(true);callbacks.current.onSavingChange(true);setError('');setSuccess('');
    try {
      const next=await deleteMemory(conversation.id,confirmation.revision,confirmation.kind==='all'?undefined:confirmation.path);
      if (!mounted.current) return;
      setIndex(next); setDocument(null); setPath(''); setContent(''); setRevision(next.revision);
      setSuccess(confirmation.kind === 'all' ? '此会话的记忆已清空。已有消息和上下文仍然保留。' : '记忆文件已删除。'); setConfirmation(null);
    } catch (e) { if (mounted.current) { mutationError(e); setConfirmation(null); } } finally {savingRef.current=false;if(mounted.current){setSaving(false);callbacks.current.onSavingChange(false);}}
  };
  if (loading) return <Loader text="正在读取记忆…"/>;
  return <><ErrorNotice message={error} onDismiss={() => setError('')}/><SuccessNotice message={success}/>
    <div className="memory-toolbar"><span className={`badge ${index?.busy ? 'amber' : 'green'}`}>{index?.busy ? '执行中 · 暂时只读' : '可管理记忆'}</span><span>{index?.files.length ?? 0} 个文件 · {sizeText(index?.files.reduce((total, file) => total + file.size, 0) || 0)}</span><div className="toolbar-spacer"/><Button variant="ghost" disabled={saving || reading || dirty} onClick={() => { setError(''); void refresh(true); }}><RefreshCw size={15}/>刷新</Button><Button variant="secondary" disabled={!index || locked || dirty} onClick={() => { setNewPath(index?.files.some(file => file.path === 'MEMORY.md') ? '' : 'MEMORY.md'); setNewFile(true); }}><FilePlus2 size={15}/>新建文件</Button><Button variant="ghost" disabled={!index?.files.length || locked || dirty} onClick={() => index && setConfirmation({ kind: 'all', path: '', revision: index.revision })}><Trash2 size={15}/>清空记忆</Button></div>
    <div className="memory-layout"><aside className="memory-files" aria-label="记忆文件"><div className="memory-files-heading"><BookOpen size={16}/><h2>{conversation.title||'新会话'}的记忆</h2></div>{index?.files.length ? index.files.map(file => <button type="button" key={file.path} disabled={saving || reading || dirty} className={`memory-file-item ${file.path === path ? 'selected' : ''}`} onClick={() => { setSuccess(''); void readFile(file.path); }}><FileText size={16}/><span><strong>{file.path}</strong><small>{sizeText(file.size)} · {timeText(file.updated_at)}</small></span></button>) : <p className="section-help memory-no-files">暂无记忆文件。你可以创建 MEMORY.md，也可以在此会话中让助手记住需要持续使用的信息。</p>}</aside>
      <section className="memory-editor">{reading ? <Loader text="正在读取文件…"/> : path ? <><div className="memory-editor-heading"><div><h2>{path}</h2><p>{document ? `更新于 ${timeText(document.updated_at)}` : '新文件 · 尚未保存'}{dirty ? ' · 有未保存修改' : ''}</p></div>{document && <Button variant="ghost" disabled={locked || dirty} onClick={() => setConfirmation({ kind: 'file', path, revision })}><Trash2 size={15}/>删除文件</Button>}</div><Field label="记忆内容" hint="填写本会话持续使用的偏好、约定或业务背景。不要在这里存放账号密码。"><textarea className="memory-content" value={content} onChange={event => setContent(event.target.value)} readOnly={locked} spellCheck={false} rows={17} placeholder="# 记忆\n\n记录希望本会话后续任务继续使用的信息。"/></Field><div className="memory-editor-footer"><span>{sizeText(bytes)} / {sizeText(index?.max_file_bytes || 131072)}</span><div className="memory-editor-actions"><Button variant="secondary" disabled={saving || reading} onClick={() => { setError(''); setSuccess(''); void refresh(true); }}>放弃修改并刷新</Button><Button busy={saving} disabled={!index || locked || bytes > index.max_file_bytes || (!!document && !dirty)} onClick={() => void save()}><Save size={15}/>保存记忆</Button></div></div></> : <EmptyState icon={<BookOpen size={30}/>} title="选择或创建记忆文件" description="MEMORY.md 是这个会话的记忆入口；相关 Markdown 文件可保存更具体的信息。" action={<Button disabled={!index || locked} onClick={() => { setNewPath('MEMORY.md'); setNewFile(true); }}><FilePlus2 size={15}/>创建记忆文件</Button>}/>}</section>
    </div><p className="memory-boundary">文件修改只影响所选会话的记忆。新会话从空白记忆开始；已进入本会话上下文的信息不会因删除记忆文件而消失。</p>
    {newFile && <Modal title="新建记忆文件" description="只接受相对路径的 Markdown 文件，例如 MEMORY.md 或 topics/preferences.md。" onClose={() => setNewFile(false)}><form onSubmit={event => { event.preventDefault(); if (!index) return; const next = newPath.trim(); if (index.files.some(file => file.path === next)) { setError('文件已存在，请在左侧选择并编辑。'); setNewFile(false); return; } setDocument(null); setPath(next); setContent(''); setRevision(index.revision); setNewFile(false); setError(''); }}><div className="modal-body"><Field label="文件路径" required><input value={newPath} onChange={event => setNewPath(event.target.value)} required pattern="[^\\]+\.md" maxLength={240} placeholder="MEMORY.md" autoFocus/></Field></div><footer className="modal-footer"><Button variant="secondary" onClick={() => setNewFile(false)}>取消</Button><Button type="submit" disabled={!newPath.trim()}>开始编辑</Button></footer></form></Modal>}
    {confirmation && <Modal title={confirmation.kind === 'all' ? '清空此会话的记忆' : '删除记忆文件'} onClose={() => { if (!saving) setConfirmation(null); }}><div className="modal-body"><p>{confirmation.kind === 'all' ? `将删除会话「${conversation.title||'新会话'}」的全部记忆文件。` : `将删除「${confirmation.path}」。`}此操作无法撤销，请先保留仍需要的内容。</p><p className="section-help">其他会话的记忆不受影响。这个会话的消息、附件与已有上下文不会删除。</p></div><footer className="modal-footer"><Button variant="secondary" disabled={saving} onClick={() => setConfirmation(null)}>保留记忆</Button><Button variant="danger" busy={saving} disabled={!!index?.busy} onClick={() => void remove()}>{confirmation.kind === 'all' ? '确认清空' : '确认删除'}</Button></footer></Modal>}
  </>;
}
