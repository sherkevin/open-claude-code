import { useCallback, useEffect, useRef, useState } from 'react';
import { Archive, ArrowDown, ArrowUp, Bot, Check, CheckCircle2, ChevronDown, Circle, Clock3, Download, FileText, Gauge, LoaderCircle, Minimize2, Paperclip, Pencil, Radio, ShieldCheck, Sparkles, Square, X, XCircle } from 'lucide-react';
import { api, API_BASE, ApiError, errorText, fileURL, patch, post, sizeText, timeText } from './api';
import type { AppConfig, Approval, Assistant, Conversation, Message, ModelOption, ModelRecommendation, ModelSelection, Run, RunStatus, Skill, StoredFile, StreamEvent } from './types';
import { isActive, statusLabels } from './types';
import { appendEvent, eventTypes, pendingApprovalIds, streamText } from './stream';
import { Attachment, Button, ErrorNotice, Field, FileChip, FilePreview, Loader, Modal } from './components';
import { compactionStatus, compactionStatusLabel, toolLabel } from './display';
import ContextDialog, { compactionLabel } from './ContextDialog';
import ModelSelector from './ModelSelector';
import { conversationHasModelHistory, modelSwitchRequiresNewConversation, publicModelLabel, saveModelSelection } from './models';
import ModelRecovery from './ModelRecovery';
import { AnswerMarkdown } from './Sources';
import SkillPicker, { intentCompletion, mentionQuery, selectedSkillIds, SkillChips } from './SkillPicker';

export default function ChatPage({ conversation, assistant, config, models, modelsLoading, onChanged, onArchived }: { conversation: Conversation; assistant?: Assistant; config: AppConfig; models: ModelOption[]; modelsLoading: boolean; onChanged: () => void; onArchived: () => void }) {
  const [messages, setMessages] = useState<Message[]>([]); const [run, setRun] = useState<Run | null>(null); const [events, setEvents] = useState<StreamEvent[]>([]);
  const [archived,setArchived]=useState(conversation.archived);const [restoring,setRestoring]=useState(false);
  const [showContext, setShowContext] = useState(false);
  const [skills, setSkills] = useState<Skill[]>([]); const [selectedSkills, setSelectedSkills] = useState<string[]>([]); const [skillQuery, setSkillQuery] = useState<string|null>(null);const [skillHighlight,setSkillHighlight]=useState(0); const mention = useRef<ReturnType<typeof mentionQuery>>(null); const [runStage,setRunStage]=useState('');
  useEffect(()=>{let current=true;api<Skill[]>(`/assistants/${conversation.assistant_id}/skills`).then(result=>{if(current)setSkills(result);}).catch(()=>{});return()=>{current=false;};},[conversation.assistant_id]);
  const [selection, setSelection] = useState<ModelSelection>({ model_id: conversation.model_id, reasoning_level: conversation.reasoning_level });
  const [recommendOpen, setRecommendOpen] = useState(false); const [recommendSummary, setRecommendSummary] = useState(''); const [recommendation, setRecommendation] = useState<ModelRecommendation | null>(null); const [recommendBusy, setRecommendBusy] = useState(false);
  const [savingModel, setSavingModel] = useState(false); const selectionVersion = useRef(0);
  const [hasNativeSession, setHasNativeSession] = useState(conversation.has_native_session);
  const [modelReconnectRequired, setModelReconnectRequired] = useState(false);
  const [refreshingRun, setRefreshingRun] = useState(false);
  const [loading, setLoading] = useState(true); const [error, setError] = useState(''); const [input, setInput] = useState(''); const [sending, setSending] = useState(false); const [uploading, setUploading] = useState(false);
  const [files, setFiles] = useState<StoredFile[]>([]); const [preview, setPreview] = useState<StoredFile | null>(null); const [title, setTitle] = useState(conversation.title); const [titleDraft, setTitleDraft] = useState(conversation.title); const titleVersion = useRef(0); const [rename, setRename] = useState(false); const [archive, setArchive] = useState(false); const [savingTitle, setSavingTitle] = useState(false); const [showFeed, setShowFeed] = useState(false); const [connectionState, setConnectionState] = useState<'connected' | 'reconnecting'>('connected'); const [stopping, setStopping] = useState(false); const [decisionBusy, setDecisionBusy] = useState('');
  const scroll = useRef<HTMLDivElement>(null); const shouldFollow = useRef(true); const [showScroll, setShowScroll] = useState(false); const fileInput = useRef<HTMLInputElement>(null); const textInput = useRef<HTMLTextAreaElement>(null); const request = useRef<{ signature: string; id: string } | null>(null); const mounted = useRef(true); const onChangedRef = useRef(onChanged); onChangedRef.current = onChanged;
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const reloadMessages = useCallback(async () => { const next = await api<Message[]>(`/conversations/${conversation.id}/messages`); if (mounted.current) setMessages(next); return next; }, [conversation.id]);
  useEffect(() => {
    let current = true;
    const requestedTitleVersion = titleVersion.current;
    const requestedSelectionVersion = selectionVersion.current;
    const load = async () => {
      try {
        const [next, detail] = await Promise.all([api<Message[]>(`/conversations/${conversation.id}/messages`), api<Conversation>(`/conversations/${conversation.id}`)]);
        if (!current) return;
        setMessages(next);
        setHasNativeSession(detail.has_native_session);setArchived(detail.archived);
        // A rename can finish before this initial GET. Its older snapshot must not undo it.
        if (titleVersion.current === requestedTitleVersion) setTitle(detail.title);
        if (selectionVersion.current === requestedSelectionVersion) setSelection({ model_id: detail.model_id, reasoning_level: detail.reasoning_level });
        const runId = detail.active_run_id || [...next].reverse().find(m => m.run_id)?.run_id;
        if (runId) { const active = await api<Run>(`/runs/${runId}`); if (current) setRun(active); }
      } catch (e) { if (current) setError(errorText(e)); }
      finally { if (current) setLoading(false); }
    };
    void load();
    return () => { current = false; };
  }, [conversation.id]);
  useEffect(() => {
    if (!run?.id) return;
    const runId = run.id; let current = true; let finalizing = false; const seen = new Set<number>(); setEvents([]); setConnectionState('connected');
    const source = new EventSource(`${API_BASE}/runs/${encodeURIComponent(runId)}/events?after=0`, { withCredentials: true });
    const finalize = async () => { if (finalizing || !current) return; finalizing = true; setRefreshingRun(true); source.close(); try { const [nextRun, , detail] = await Promise.all([api<Run>(`/runs/${runId}`), reloadMessages(), api<Conversation>(`/conversations/${conversation.id}`)]); if (current) { setRun(nextRun); setHasNativeSession(detail.has_native_session); setConnectionState('connected'); onChangedRef.current(); } } catch (e) { if (current) setError(errorText(e)); } finally { finalizing = false; if (current) setRefreshingRun(false); } };
    const receive = (raw: Event) => {
      if (!current) return;
      let event: StreamEvent;
      try { event = JSON.parse((raw as MessageEvent).data) as StreamEvent; } catch { setError('收到无法识别的执行事件，请刷新对话重新读取。'); return; }
      if (event.run_id !== runId || seen.has(event.id)) return; seen.add(event.id);
      setEvents(prev => appendEvent(prev, event));
      if (event.type === 'timing.updated') setRun(prev => prev ? {...prev,timings:{...prev.timings,...event.data}} : prev);
      if (event.type === 'run.stage') setRunStage(String(event.data.stage||''));
      if (event.type === 'usage.updated') setRun(prev => prev ? { ...prev, usage: event.data } : prev);
      if (event.type === 'run.started') {setRunStage('initializing');}
      if (event.type === 'run.started') setRun(prev => prev ? { ...prev, status: 'running' } : prev);
      if (event.type === 'approval.required') { setRun(prev => prev ? { ...prev, status: 'awaiting_approval' } : prev); setShowFeed(true); }
      if (event.type === 'approval.resolved') setRun(prev => prev ? { ...prev, status: 'running', approvals: prev.approvals.filter(a => a.id !== event.data.approval_id) } : prev);
      if (['context.compaction_started', 'context.compacted', 'context.compaction_result'].includes(event.type)) setRun(prev => prev ? { ...prev, compaction: { trigger: null, pre_tokens: null, before: null, after: null, ...prev.compaction, status: event.type === 'context.compaction_started' ? 'started' : event.type === 'context.compacted' ? 'completed' : compactionStatus(event.data.status), error: typeof event.data.error === 'string' ? event.data.error : null, followup_status: typeof event.data.followup_status === 'string' ? event.data.followup_status : prev.compaction?.followup_status } } : prev);
      if (event.type === 'run.completed') { setRun(prev => prev ? { ...prev, status: event.data.status as RunStatus, error: typeof event.data.error === 'string' ? event.data.error : null } : prev); void finalize(); }
    };
    eventTypes.forEach(type => source.addEventListener(type, receive));
    source.onopen = () => { if (current) setConnectionState('connected'); };
    source.onerror = () => { if (!current) return; setConnectionState('reconnecting'); void api<Run>(`/runs/${runId}`).then(next => { if (!current) return; if (!isActive(next.status)) void finalize(); else setRun(next); }).catch(() => {}); };
    return () => { current = false; source.close(); };
  // Only a different run starts a different event subscription.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run?.id, reloadMessages]);
  const liveText = streamText(events); const active = !!run && isActive(run.status); const pendingIds = pendingApprovalIds(events);
  const selectedModel = models.find(model => model.id === selection.model_id);
  const selectedModelLabel = publicModelLabel(models, selection.model_id);
  const selectedReasoningLabel = selectedModel?.reasoning_levels.find(level => level.id === selection.reasoning_level)?.label;
  const modelReady = !!selectedModel?.available && !!selectedReasoningLabel && !modelsLoading;
  const hasHistory = conversationHasModelHistory(hasNativeSession, messages.length);
  const approvalsFromEvents = events.filter(e => e.type === 'approval.required' && pendingIds.has(String(e.data.id))).map(e => e.data as unknown as Approval);
  const approvals = active ? [...new Map([...(run?.approvals || []).filter(a => a.status === 'pending'), ...approvalsFromEvents].map(a => [a.id, a])).values()].filter(a => !events.some(e => e.type === 'approval.resolved' && e.data.approval_id === a.id)) : [];
  const canonicalHasAnswer = messages.some(m => m.role === 'assistant' && m.run_id === run?.id && !!m.text);
  const liveCitations=events.filter(e=>e.type==='message.updated').at(-1)?.data.citations as Message['citations'];
  const showStream = !!run && (active || !canonicalHasAnswer) && !!liveText;
  const artifacts = events.filter(e => e.type === 'artifact.created').map(e => e.data as unknown as StoredFile);
  useEffect(() => { if (shouldFollow.current && scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight; }, [messages, liveText, loading]);
  useEffect(() => { if (textInput.current) { textInput.current.style.height = 'auto'; textInput.current.style.height = `${Math.min(textInput.current.scrollHeight, 160)}px`; } }, [input]);
  const matchingSkills=skills.filter(skill=>skill.enabled&&!selectedSkills.includes(skill.id)&&(skill.name+' '+skill.description).toLowerCase().includes((skillQuery||'').toLowerCase()));
  const intentSuggestion = intentCompletion(input);
  useEffect(()=>{setSkillHighlight(0);},[skillQuery]);
  const addSkill=(skill:Skill)=>{if(selectedSkills.length>=5)return;setSelectedSkills(prev=>[...new Set([...prev,skill.id])].slice(0,5));if(mention.current){const {start,end}=mention.current;setInput(prev=>prev.slice(0,start)+prev.slice(end));}setSkillQuery(null);mention.current=null;textInput.current?.focus();};
  const send = async () => {
    if ((!input.trim() && files.length === 0 && selectedSkills.length===0) || archived || active || sending || uploading || loading || savingModel || showContext || !modelReady) return;
    const text = input.trim() || (files.length?'请阅读我上传的附件。':'请执行所选 Skill。'); const fileIds = files.map(f => f.id); const skillIds = selectedSkillIds(selectedSkills,skills); const signature = JSON.stringify([text, fileIds,skillIds]);
    if (!request.current || request.current.signature !== signature) request.current = { signature, id: crypto.randomUUID() };
    setSending(true); setError(''); shouldFollow.current = true;
    try {
      const result = await post<{ message: Message; run_id: string }>(`/conversations/${conversation.id}/messages`, { client_request_id: request.current.id, text, file_ids: fileIds, skill_ids:skillIds });
      if (!mounted.current) return;
      setMessages(previous => previous.some(m => m.id === result.message.id) ? previous : [...previous, result.message]); setInput(''); setFiles([]); setSelectedSkills([]); setSkillQuery(null);setRunStage('initializing'); request.current = null;
      setRun({ id: result.run_id, conversation_id: conversation.id, status: 'queued', assistant_message_id: null, error: null, usage: null, approvals: [], created_at: new Date().toISOString(), completed_at: null });
      const next = await api<Run>(`/runs/${result.run_id}`); if (!mounted.current) return; setRun(next); onChangedRef.current();
    } catch (e) { if (mounted.current) { setError(errorText(e)); if (e instanceof ApiError && e.code === 'MODEL_SELECTION_REQUIRED') setModelReconnectRequired(true); } } finally { if (mounted.current) setSending(false); }
  };
  const upload = async (selectedFiles: FileList | null) => { if (!selectedFiles) return; setUploading(true); setError(''); try { for (const file of Array.from(selectedFiles)) { if (file.size > config.upload_max_bytes) throw new Error(`「${file.name}」超过 ${sizeText(config.upload_max_bytes)} 的附件上限。`); const body = new FormData(); body.append('file', file); const saved = await api<StoredFile>('/files', { method: 'POST', body }); if (mounted.current) setFiles(prev => [...prev, saved]); } } catch (e) { if (mounted.current) setError(errorText(e)); } finally { if (mounted.current) setUploading(false); if (fileInput.current) fileInput.current.value = ''; } };
  const decide = async (approval: Approval, decision: 'allow' | 'deny') => { setDecisionBusy(approval.id); setError(''); try { await post(`/approvals/${approval.id}/decision`, { decision }); if (run) setRun(await api<Run>(`/runs/${run.id}`)); } catch (e) { setError(errorText(e)); } finally { setDecisionBusy(''); } };
  const stop = async () => { if (!run) return; setStopping(true); try { await post(`/runs/${run.id}/cancel`); setRun(await api<Run>(`/runs/${run.id}`)); } catch (e) { setError(errorText(e)); } finally { setStopping(false); } };
  const saveTitle = async () => {
    const nextTitle = titleDraft.trim();
    if (!nextTitle || savingTitle) return;
    titleVersion.current += 1;
    setSavingTitle(true);
    try {
      const saved = await patch<Conversation>(`/conversations/${conversation.id}`, { title: nextTitle });
      if (!mounted.current) return;
      setTitle(saved.title);
      setRename(false);
      onChangedRef.current();
    } catch (err) { if (mounted.current) setError(errorText(err)); }
    finally { if (mounted.current) setSavingTitle(false); }
  };
  const changeModel = async (next: ModelSelection, reconnect = false) => {
    if (active || loading || sending || savingModel || showContext || uploading || refreshingRun) return;
    if (!reconnect && next.model_id === selection.model_id && next.reasoning_level === selection.reasoning_level) return;
    if (!models.some(model => model.id === next.model_id && model.available && model.reasoning_levels.some(level => level.id === next.reasoning_level))) return;
    if (modelSwitchRequiresNewConversation(models, selection.model_id, next.model_id, hasHistory)) {
      setError('请新建对话使用此模型，当前对话和记忆保留。'); return;
    }
    selectionVersion.current += 1; setSavingModel(true); setError('');
    try {
      const saved = await saveModelSelection(conversation.id, selection, next, reconnect);
      if (!mounted.current || !saved) return;
      // The successful server response is authoritative; a rejected change keeps the current selection.
      setSelection({ model_id: saved.model_id, reasoning_level: saved.reasoning_level });
      setHasNativeSession(saved.has_native_session);
      setModelReconnectRequired(false); setError('');
      request.current = null;
      onChangedRef.current();
    } catch (e) { if (mounted.current) { setError(errorText(e)); if (e instanceof ApiError && e.code === 'MODEL_SELECTION_REQUIRED') setModelReconnectRequired(true); } }
    finally { if (mounted.current) setSavingModel(false); }
  };
  const recommend = async (summaryOverride?: string) => {
    const summary = (summaryOverride ?? recommendSummary).trim();
    if (!summary || recommendBusy || active) { if (!summary && mounted.current) setError('请先在输入框描述当前任务，再选择最适合的模型。'); return; }
    setRecommendBusy(true); setError(''); setRecommendSummary(summary);
    try {
      const next = await post<ModelRecommendation>('/models/recommend', { task_summary: summary, candidate_model_ids: models.filter(model => model.available).map(model => model.id) });
      if (!mounted.current) return;
      setRecommendation(next);
      // The operation is deliberately explicit (the user clicked the button),
      // then applies the bounded recommendation without a second confirmation.
      await changeModel({ model_id: next.model_id, reasoning_level: next.reasoning_level });
    }
    catch (e) { if (mounted.current) setError(errorText(e)); }
    finally { if (mounted.current) setRecommendBusy(false); }
  };
  return <div className="chat-page"><header className="chat-header"><div className="chat-heading"><span className="assistant-icon small"><Bot size={21}/></span><div><h1>{title || '新对话'}</h1><span>{assistant?.name || '业务助手'}{active && <><i/> {statusLabels[run.status]}</>}</span></div></div><div className="chat-actions"><button className="icon-button" title="重命名对话" aria-label="重命名对话" onClick={() => { setTitleDraft(title); setRename(true); }}><Pencil size={16}/></button><button type="button" className="icon-button" title="上下文" aria-label="上下文" disabled={savingModel || loading} onClick={() => setShowContext(true)}><Gauge size={17}/></button><button className="icon-button" title="归档对话" aria-label="归档对话" disabled={active} onClick={() => setArchive(true)}><Archive size={17}/></button><button className={`feed-toggle ${showFeed ? 'selected' : ''}`} onClick={() => setShowFeed(!showFeed)}><Radio size={16}/><span>执行记录</span>{active && <span className="tiny-dot"/>}</button></div></header>
    <div className="chat-layout"><section className="conversation-column"><div className="message-scroll" ref={scroll} onScroll={() => { const el = scroll.current; if (!el) return; shouldFollow.current = el.scrollHeight - el.scrollTop - el.clientHeight < 120; setShowScroll(!shouldFollow.current); }}>
      {loading ? <Loader text="正在恢复对话…"/> : messages.length === 0 && !liveText ? <div className="chat-intro"><span className="assistant-icon large"><Bot size={30}/></span><h2>和{assistant?.name || '业务助手'}一起开始</h2><p>{assistant?.description || '描述你的问题或目标，也可以上传相关资料。'}</p><div className="chat-intro-note"><ShieldCheck size={16}/>调用需审批的业务工具时，你会先看到操作内容。</div></div> : <div className="messages">{messages.filter(m => !(active && m.role === 'assistant' && m.run_id === run?.id) && (m.role !== 'assistant' || !!m.text || m.file_ids.length > 0)).map(message => <article className={`message ${message.role}`} key={message.id}><div className="message-label">{message.role === 'assistant' ? <><Bot size={16}/>{assistant?.name || '助手'}</> : <>你<span>{timeText(message.created_at)}</span></>}</div><div className="message-content">{message.role === 'assistant' ? <AnswerMarkdown text={message.text} citations={message.citations}/> : <p className="user-text">{run?.kind === 'compact' && message.run_id === run.id ? '请求原生上下文压缩' : message.text}</p>}{message.role==='user'&&!!message.skills?.length&&<div className="message-skill-record">{message.skills.map(skill=><span key={skill.id}>@{skill.name} · v{skill.revision}</span>)}</div>}{message.file_ids.length > 0 && <div className="attachment-row">{message.file_ids.map(id => <Attachment key={id} id={id} onPreview={setPreview}/>)}</div>}</div></article>)}{showStream && <article className="message assistant"><div className="message-label"><Bot size={16}/>{assistant?.name || '助手'}{active && <span className="typing-indicator"><i/><i/><i/></span>}</div><div className="message-content"><AnswerMarkdown text={liveText} citations={liveCitations}/></div></article>}{active && <LiveActivity run={run} events={events} stage={runStage} connectionState={connectionState}/>}<div className="compaction-message-container">{run?.kind === 'compact' && <div className="compaction-message"><Minimize2 size={17}/><div><strong>{compactionLabel(run)}</strong><p>历史消息与附件仍保留，可在“上下文”中查看原生统计。</p></div></div>}</div>{run && !active && ['failed', 'interrupted', 'cancelled'].includes(run.status) && <div className="run-outcome"><span className={`badge ${run.status === 'failed' ? 'amber' : ''}`}>{statusLabels[run.status]}</span><p>{run.error || (run.status === 'interrupted' ? '服务重启或连接中断。你可以继续发送消息恢复会话，之前的业务操作不会自动重做。' : '你可以继续发送消息。已执行的业务操作不会自动撤销。')}</p></div>}{artifacts.length > 0 && <div className="artifact-section"><span className="eyebrow">本次工作产物</span><div className="attachment-row">{artifacts.map(file => <FileChip key={file.id} file={file} onPreview={() => setPreview(file)}/>)}</div></div>}</div>}
    </div>{showScroll && <button className="scroll-latest" onClick={() => { if (scroll.current) scroll.current.scrollTo({ top: scroll.current.scrollHeight, behavior: 'smooth' }); shouldFollow.current = true; }}><ArrowDown size={15}/>回到最新消息</button>}
    <div className="composer-area">{archived&&<div className="archive-banner"><span>此会话已归档，恢复后可以继续工作。</span><Button variant="secondary" busy={restoring} onClick={async()=>{setRestoring(true);try{await patch(`/conversations/${conversation.id}`,{archived:false});setArchived(false);onChangedRef.current();}catch(e){setError(errorText(e));}finally{setRestoring(false);}}}>恢复会话</Button></div>}<ModelSelector models={models} value={selection} hasHistory={hasHistory} loading={modelsLoading} disabled={archived || active || loading || sending || uploading || showContext || refreshingRun} pending={savingModel} onRecommend={() => { const summary = input.trim(); setRecommendSummary(summary); setRecommendation(null); setRecommendOpen(true); void recommend(summary); }} recommendBusy={recommendBusy} onChange={next => void changeModel(next)}/><ErrorNotice message={error} onDismiss={() => setError('')}/>{modelReconnectRequired && <ModelRecovery currentAvailable={modelReady} busy={active || loading || sending || savingModel || uploading || showContext || refreshingRun} pending={savingModel} onReconnect={() => void changeModel(selection, true)}/>}{approvals.length > 0 && !showFeed && <button className="approval-banner" onClick={() => setShowFeed(true)}><ShieldCheck size={17}/>有 {approvals.length} 项操作等待你的确认<ArrowUp size={16}/></button>}<form className="composer" onSubmit={e => { e.preventDefault(); void send(); }}>
      {skillQuery!==null&&<SkillPicker skills={skills} selected={selectedSkills} query={skillQuery} onClose={()=>{setSkillQuery(null);mention.current=null;}} highlighted={skillHighlight} onSelect={addSkill}/>}<SkillChips selected={selectedSkills} skills={skills} disabled={sending} onRemove={id=>setSelectedSkills(prev=>prev.filter(v=>v!==id))}/>
      {files.length > 0 && <div className="attachment-row composer-files">{files.map(file => <FileChip key={file.id} file={file} onPreview={() => setPreview(file)} onRemove={() => setFiles(prev => prev.filter(f => f.id !== file.id))}/>)}</div>}{intentSuggestion && !skillQuery && <div className="intent-suggestion" role="status">Tab 接受意图补全：{intentSuggestion}</div>}<textarea ref={textInput} aria-label="输入消息" placeholder={active ? '助手正在工作，你可以先准备下一条消息…' : '描述你要完成的工作，输入 @ 选择 Skill…'} value={input} onChange={e => {setInput(e.target.value);mention.current=mentionQuery(e.target.value,e.target.selectionStart);setSkillQuery(mention.current?.query??null);}} onKeyDown={e => { if(e.key==='Tab'&&!e.nativeEvent.isComposing&&skillQuery===null&&intentSuggestion){e.preventDefault();setInput(intentSuggestion);requestAnimationFrame(()=>{const node=textInput.current;if(node){node.focus();node.setSelectionRange(intentSuggestion.length,intentSuggestion.length);}});return;} if(skillQuery!==null&&!e.nativeEvent.isComposing&&['ArrowDown','ArrowUp','Enter'].includes(e.key)){e.preventDefault();if(e.key==='Enter'){const choice=matchingSkills[skillHighlight];if(choice)addSkill(choice);else setSkillQuery(null);}else if(matchingSkills.length)setSkillHighlight(previous=>(previous+(e.key==='ArrowDown'?1:-1)+matchingSkills.length)%matchingSkills.length);return;} if(e.key==='Escape'&&skillQuery!==null){e.preventDefault();setSkillQuery(null);mention.current=null;return;} if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); void send(); } }} rows={1}/><div className="composer-toolbar"><input ref={fileInput} type="file" multiple hidden onChange={e => void upload(e.target.files)}/><button className="attach-button" type="button" disabled={uploading || sending || loading} onClick={() => fileInput.current?.click()}>{uploading ? <LoaderCircle className="spin" size={18}/> : <Paperclip size={18}/>}<span>{uploading ? '上传中' : '添加附件'}</span></button><button type="button" className="attach-button" disabled={sending||loading} onClick={()=>{mention.current=null;setSkillQuery(previous=>previous===null?'':null);}}><Sparkles size={16}/><span>Skill</span></button><span className="composer-keyhint">Enter 发送 · Shift + Enter 换行</span>{active ? <Button type="button" variant="secondary" busy={stopping} onClick={() => void stop()} disabled={run.status === 'cancelling'}><Square size={13}/>{run.status === 'cancelling' ? '停止中' : '停止'}</Button> : <button className="send-button" type="submit" aria-label="发送消息" disabled={archived || sending || uploading || loading || savingModel || showContext || !modelReady || (!input.trim() && !files.length && !selectedSkills.length)}>{sending ? <LoaderCircle size={18} className="spin"/> : <ArrowUp size={20}/>}</button>}</div></form><div className="composer-footnote"><span>{selectedModelLabel}{selectedReasoningLabel && ` · ${selectedReasoningLabel}`}</span><span>数据 · 知识 · 网页来源可核验</span></div></div></section>
    {showFeed && <aside className="execution-panel"><header><div><Radio size={16}/><h2>执行记录</h2></div><button className="icon-button" aria-label="关闭执行记录" onClick={() => setShowFeed(false)}><X size={17}/></button></header><div className="execution-body">{run ? <><div className="execution-state"><span className={`badge ${run.status === 'succeeded' ? 'green' : run.status === 'failed' || run.status === 'awaiting_approval' ? 'amber' : ''}`}>{active && <LoaderCircle size={12} className="spin"/>}{statusLabels[run.status]}</span>{connectionState === 'reconnecting' && active && <small>连接断开，正在自动重连…</small>}</div><RunMetrics run={run}/>{approvals.map(approval => <ApprovalCard key={approval.id} approval={approval} busy={decisionBusy === approval.id} onDecision={decision => void decide(approval, decision)}/>)}<div className="event-list">{events.filter(e => ['run.started', 'tool.started', 'tool.completed', 'approval.resolved', 'artifact.created', 'context.compaction_started', 'context.compacted', 'context.compaction_result', 'run.completed'].includes(e.type)).map(event => <EventRow key={event.id} event={event}/>)}</div>{!events.length && <p className="section-help">正在读取执行记录…</p>}</> : <div className="execution-empty"><Clock3 size={26}/><h3>每一步，都有记录</h3><p>发送消息后，这里会展示工具调用、待确认操作和执行结果。</p></div>}</div></aside>}
    </div>{showContext && <ContextDialog conversationId={conversation.id} models={models} busy={active || sending || uploading || savingModel || loading} run={run} onClose={() => setShowContext(false)} onCompact={result => { setShowContext(false); setShowFeed(true); setMessages(previous => previous.some(message => message.id === result.message.id) ? previous : [...previous, result.message]); setRun({ id: result.run_id, conversation_id: conversation.id, status: 'queued', kind: 'compact', compaction: null, assistant_message_id: null, error: null, usage: null, approvals: [], created_at: new Date().toISOString(), completed_at: null }); void api<Run>(`/runs/${result.run_id}`).then(next => { if (mounted.current) setRun(next); }).catch(e => { if (mounted.current) setError(errorText(e)); }); onChangedRef.current(); }}/>} {preview && <FilePreview key={preview.id} file={preview} onClose={() => setPreview(null)}/>}{rename && <Modal title="重命名对话" onClose={() => { if (!savingTitle) setRename(false); }}><form onSubmit={e => { e.preventDefault(); void saveTitle(); }}><div className="modal-body"><Field label="对话名称"><input value={titleDraft} onChange={e => setTitleDraft(e.target.value)} disabled={savingTitle} maxLength={200} required autoFocus/></Field></div><footer className="modal-footer"><Button type="button" variant="secondary" disabled={savingTitle} onClick={() => setRename(false)}>取消</Button><Button busy={savingTitle} disabled={!titleDraft.trim()} type="submit">保存名称</Button></footer></form></Modal>}{archive && <Modal title="归档此对话" onClose={() => setArchive(false)}><div className="modal-body"><p>归档后，对话会从最近列表中移除。对话和附件仍保存在服务端。</p></div><footer className="modal-footer"><Button variant="secondary" onClick={() => setArchive(false)}>取消</Button><Button onClick={async () => { try { await patch(`/conversations/${conversation.id}`, { archived: true }); onChangedRef.current(); onArchived(); } catch (e) { setError(errorText(e)); setArchive(false); } }}>归档对话</Button></footer></Modal>}{recommendOpen && <Modal title="为当前任务推荐模型" onClose={() => { if (!recommendBusy) setRecommendOpen(false); }}><div className="modal-body"><p>系统只使用当前任务摘要进行 bounded 推荐，不读取或发送完整会话历史。点击“Select the most fitness model”后，推荐结果会自动应用到当前空闲会话。</p><Field label="任务摘要"><textarea value={recommendSummary} onChange={e => setRecommendSummary(e.target.value)} maxLength={2000} rows={4} placeholder="例如：分析一份中文质量报告并给出改进建议。" disabled={recommendBusy} autoFocus/></Field>{recommendation && <div className="recommendation-result"><strong>{recommendation.model_label} · {recommendation.reasoning_level}</strong><p>{recommendation.reason}</p><small>路由器：{recommendation.router.model_id} · 输入 {recommendation.router.input_chars} 字符 · 仅供参考</small></div>}</div><footer className="modal-footer"><Button type="button" variant="secondary" disabled={recommendBusy} onClick={() => setRecommendOpen(false)}>关闭</Button>{recommendation ? <Button type="button" onClick={() => setRecommendOpen(false)}>完成</Button> : <Button type="button" busy={recommendBusy} disabled={!recommendSummary.trim()} onClick={() => void recommend()}>生成推荐</Button>}</footer></Modal>}</div>;
}

function LiveActivity({ run, events, stage, connectionState }: { run: Run; events: StreamEvent[]; stage: string; connectionState: 'connected' | 'reconnecting' }) {
  const toolFinished = new Set(events.filter(event => event.type === 'tool.completed').map(event => String(event.data.tool_call_id || event.id)));
  const pendingTool = [...events].reverse().find(event => event.type === 'tool.started' && !toolFinished.has(String(event.data.tool_call_id || event.id)));
  const latestTool = [...events].reverse().find(event => event.type === 'tool.completed');
  const latestApproval = [...events].reverse().find(event => event.type === 'approval.required');
  const latestCompaction = [...events].reverse().find(event => event.type.startsWith('context.compaction'));
  let title = '正在处理当前任务';
  let detail = '执行记录会随着真实事件更新。';
  let icon = <LoaderCircle className="spin" size={16}/>;
  let waiting = false;
  if (connectionState === 'reconnecting') {
    title = '连接暂时中断'; detail = '正在恢复执行记录，不会重复发起已经记录的工具调用。'; icon = <Radio size={16}/>;
  } else if (run.execution_kind === 'product_metadata') {
    title = '正在读取模型信息'; detail = '这是产品信息查询，不经过模型推理。'; icon = <CheckCircle2 size={16}/>;
  } else if (run.status === 'awaiting_approval') {
    const name = String(latestApproval?.data.tool_name || run.approvals.at(-1)?.tool_name || '这项操作');
    title = '等待你的确认'; detail = `准备执行 ${toolLabel(name)}，允许后才会继续。`; icon = <ShieldCheck size={16}/>; waiting = true;
  } else if (run.status === 'cancelling') {
    title = '正在停止当前任务'; detail = '正在等待执行中的工具返回终态。'; icon = <Square size={15}/>;
  } else if (latestCompaction && latestCompaction.type === 'context.compaction_started') {
    title = '正在整理上下文'; detail = '原生上下文压缩已开始，完成后会继续当前任务。'; icon = <Minimize2 size={16}/>;
  } else if (pendingTool) {
    title = `正在调用 ${toolLabel(String(pendingTool.data.tool_name || '业务工具'))}`;
    detail = '工具请求已发出，等待返回可核验结果。'; icon = <ArrowUp size={16}/>;
  } else if (latestTool && stage === 'tool') {
    title = `已取得 ${toolLabel(String(latestTool.data.tool_name || '业务工具'))} 返回`;
    const facts = latestTool.data.result_facts && typeof latestTool.data.result_facts === 'object' ? latestTool.data.result_facts as Record<string, unknown> : {};
    const sourceCount = Array.isArray(latestTool.data.source_ids) ? latestTool.data.source_ids.length : Number(facts.source_count || 0);
    detail = latestTool.data.success === false ? '工具返回失败，正在整理可用信息。' : sourceCount > 0 ? `已登记 ${sourceCount} 个可核验来源，正在整理业务结论。` : '工具已返回，正在整理业务结论。'; icon = latestTool.data.success === false ? <XCircle size={16}/> : <CheckCircle2 size={16}/>;
  } else if (stage === 'initializing' || run.status === 'queued') {
    title = '正在准备执行环境'; detail = '确认助手配置、会话上下文和可用能力。';
  } else if (stage === 'runtime_ready') {
    title = '执行环境已就绪'; detail = '即将向模型提交当前任务。'; icon = <CheckCircle2 size={16}/>;
  } else if (stage === 'model_started') {
    title = '模型已开始响应'; detail = '正在整理回答，界面只展示公开进度摘要。';
  } else if (stage === 'answering' || events.some(event => event.type === 'message.delta')) {
    title = '正在生成回答'; detail = '回答会继续流式显示，工具和来源记录会同步保留。';
  }
  const visible = [...events].filter(event => ['run.stage', 'tool.started', 'tool.completed', 'approval.required', 'approval.resolved', 'context.compaction_started', 'context.compacted'].includes(event.type)).slice(-4);
  const eventLabel = (event: StreamEvent) => {
    if (event.type === 'run.stage') return ({ initializing: '准备执行环境', runtime_ready: '执行环境就绪', model_started: '模型开始响应', answering: '开始生成回答', tool: '处理工具结果' } as Record<string, string>)[String(event.data.stage)] || '更新执行阶段';
    if (event.type === 'tool.started') return `调用 ${toolLabel(String(event.data.tool_name || '业务工具'))}`;
    if (event.type === 'tool.completed') return `${event.data.success === false ? '工具失败' : '工具完成'} · ${toolLabel(String(event.data.tool_name || '业务工具'))}`;
    if (event.type === 'approval.required') return '等待操作确认';
    if (event.type === 'approval.resolved') return event.data.decision === 'allow' ? '已允许操作' : '未获授权';
    if (event.type === 'context.compaction_started') return '开始整理上下文';
    return '上下文整理完成';
  };
  return <div className={`live-activity ${waiting ? 'waiting' : ''}`} aria-live="polite"><div className="live-activity-current"><span className="live-activity-icon">{icon}</span><div><strong>{title}</strong><p>{detail}</p></div></div>{visible.length > 0 && <div className="live-activity-trail" aria-label="当前执行脉络">{visible.map(event => <span key={event.id} className={event.type === 'tool.completed' && event.data.success === false ? 'failed' : ''}>{eventLabel(event)}</span>)}</div>}</div>;
}

function ApprovalCard({ approval, busy, onDecision }: { approval: Approval; busy: boolean; onDecision: (decision: 'allow' | 'deny') => void }) {
  const expired = new Date(approval.expires_at).getTime() < Date.now();
  return <section className="approval-card"><div className="approval-title"><ShieldCheck size={18}/><strong>请确认这项操作</strong></div><h3>{toolLabel(approval.tool_name)}</h3><details open><summary>操作参数<ChevronDown size={14}/></summary><pre>{JSON.stringify(approval.arguments, null, 2)}</pre></details><p>{expired ? '此操作的确认请求已过期。' : `有效至 ${timeText(approval.expires_at)}`}</p><div className="approval-actions"><Button variant="secondary" disabled={busy || expired} onClick={() => onDecision('deny')}><X size={15}/>拒绝</Button><Button busy={busy} disabled={expired} onClick={() => onDecision('allow')}><Check size={15}/>允许执行</Button></div></section>;
}
function EventRow({ event }: { event: StreamEvent }) {
  let label = ''; let detail = ''; let icon = <Circle size={14}/>;
  if (event.type === 'run.started') { label = '开始执行'; icon = <Radio size={14}/>; }
  if (event.type === 'tool.started') { label = toolLabel(String(event.data.tool_name || '调用工具')); icon = <ArrowUpRightIcon/>; }
  if (event.type === 'tool.completed') { label = `${event.data.success ? '完成' : '失败'} · ${toolLabel(String(event.data.tool_name || '工具调用'))}`; const facts = event.data.result_facts && typeof event.data.result_facts === 'object' ? event.data.result_facts as Record<string, unknown> : {}; const sourceCount = Array.isArray(event.data.source_ids) ? event.data.source_ids.length : Number(facts.source_count || 0); detail = sourceCount > 0 ? `已登记 ${sourceCount} 个可核验来源。` : typeof event.data.summary === 'string' ? event.data.summary : ''; icon = event.data.success ? <CheckCircle2 size={14}/> : <XCircle size={14}/>; }
  if (event.type === 'approval.resolved') { label = event.data.decision === 'allow' ? '已允许操作' : '已拒绝操作'; icon = <ShieldCheck size={14}/>; }
  if (event.type === 'artifact.created') { label = `已生成 ${String(event.data.name || '文件')}`; icon = <FileText size={14}/>; }
  if (event.type === 'context.compaction_started') { label = '开始原生压缩'; icon = <Minimize2 size={14}/>; }
  if (event.type === 'context.compacted') { label = '已发生原生上下文压缩'; icon = <CheckCircle2 size={14}/>; }
  if (event.type === 'context.compaction_result') { label = compactionStatusLabel(event.data.status, event.data.followup_status); icon = <Minimize2 size={14}/>; }
  if (event.type === 'run.completed') { label = statusLabels[event.data.status as RunStatus] || '执行结束'; icon = event.data.status === 'succeeded' ? <CheckCircle2 size={14}/> : <Square size={12}/>; }
  return <div className={`event-row ${event.type === 'tool.completed' && !event.data.success ? 'failed' : ''}`}><span className="event-icon">{icon}</span><div><span>{label}</span><small>{timeText(event.created_at)}</small>{detail && <details><summary>查看结果摘要<ChevronDown size={12}/></summary><pre>{detail}</pre></details>}{event.type === 'tool.started' && event.data.arguments != null && <details><summary>查看调用参数<ChevronDown size={12}/></summary><pre>{JSON.stringify(event.data.arguments, null, 2)}</pre></details>}{event.type === 'artifact.created' && typeof event.data.id === 'string' && <a className="inline-download" href={fileURL(event.data.id)} download><Download size={13}/>下载</a>}</div></div>;
}
function ArrowUpRightIcon() { return <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M7 17 17 7M7 7h10v10"/></svg>; }

export function RunMetrics({ run }: { run: Run }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => { if (!isActive(run.status)) return; const timer = window.setInterval(() => setNow(Date.now()), 1000); return () => window.clearInterval(timer); }, [run.status]);
  const seconds = Math.max(0, Math.floor(((run.completed_at ? new Date(run.completed_at).getTime() : now) - new Date(run.created_at).getTime()) / 1000));
  const duration = seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
  const metadata=run.execution_kind==='product_metadata';
  const usage = run.usage;
  const tokens = (value: unknown) => metadata?'0':typeof value === 'number' ? value.toLocaleString('zh-CN') : '未提供';
  const cost = metadata?0:usage?.estimated_cost_usd;
  return <section className="run-metrics" aria-label="本次运行用量"><dl>{metadata&&<><dt>执行方式</dt><dd>模型信息查询</dd><dt>模型调用</dt><dd>未调用模型</dd></>}<dt>执行耗时</dt><dd>{duration}</dd>{run.timings&&Object.entries({runtime_ready_ms:'运行环境就绪',first_model_event_ms:'首个模型事件',first_text_ms:'首段正文'}).map(([key,label])=>{const value=run.timings?.[key as keyof typeof run.timings];return typeof value==='number'?<div className="timing-row" key={key}><dt>{label}</dt><dd>{(value/1000).toFixed(2)} 秒</dd></div>:null;})}<dt>输入 Token</dt><dd>{tokens(usage?.input_tokens)}</dd><dt>输出 Token</dt><dd>{tokens(usage?.output_tokens)}</dd><dt>缓存读取 Token</dt><dd>{tokens(usage?.cache_read_input_tokens)}</dd><dt>费用估算</dt><dd>{typeof cost === 'number' ? `$${cost.toFixed(4)}` : '未提供'}</dd></dl><a className="trace-download" href={`${API_BASE}/runs/${encodeURIComponent(run.id)}/trace`} download={`run-${run.id}.json`}><Download size={14}/>下载执行记录 JSON</a></section>;
}





