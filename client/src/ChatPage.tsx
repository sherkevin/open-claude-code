import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { BookOpen, Archive, ArrowDown, ArrowUp, Check, CheckCircle2, ChevronDown, Circle, Clock3, Download, FileText, Gauge, LoaderCircle, Minimize2, Paperclip, Pencil, Radio, RefreshCw, ShieldCheck, Sparkles, Square, X, XCircle } from 'lucide-react';
import { api, API_BASE, ApiError, clientRequestId, errorText, fileURL, patch, post, timeText } from './api';
import { configAllows, type AppConfig, type Approval, type Assistant, type Conversation, type Message, type ModelOption, type ModelRecommendation, type ModelSelection, type Run, type RunStatus, type Skill, type StoredFile, type StreamEvent } from './types';
import { isActive, statusLabels } from './types';
import { appendEvent, eventTypes, pendingApprovalIds, streamText } from './stream';
import { Attachment, Button, ErrorNotice, Field, FileChip, FilePreview, Loader, Modal } from './components';
import { compactionStatus, compactionStatusLabel, toolLabel } from './display';
import ContextDialog, { compactionLabel } from './ContextDialog';
import ModelSelector from './ModelSelector';
import { ModelRoutingControl, ModelSettingsDisclosure, RunModelIdentity } from './ModelRouting';
import { conversationHasModelHistory, modelSwitchRequiresNewConversation, saveModelRouting, saveModelSelection } from './models';
import ModelRecovery from './ModelRecovery';
import RuntimeBadge from './RuntimeBadge';
import AiIdentity from './AiIdentity';
import MemoryApprovalCard, { MEMORY_WRITE_TOOL } from './MemoryApprovalCard';
import { AnswerMarkdown } from './Sources';
import BusinessResults, { questionForRun } from './BusinessResult';
import HistoricalBusinessResults, { historicalResultMessages } from './HistoricalBusinessResults';
import SkillPicker, { mentionQuery, selectedSkillIds, SkillChips } from './SkillPicker';
import { ComposerCompletion, ComposerFollowups, composerInputMaxHeight, composerShortcut, useComposerAssistance } from './ComposerAssistance';
import MessageQueue, { useMessageQueue } from './MessageQueue';
import { CHAT_ATTACHMENT_ACCEPT, chatAttachmentProblem } from './attachmentPolicy';
import { MemoryWorkspace } from './MemoryPage';
import './composer.css';

// Keep routing input bounded while deriving it from the current task context.
export const fitnessTaskSummary=(input:string,messages:Message[],assistantDescription='')=>{
  const direct=input.trim();
  const recent=messages.filter(message=>message.role==='user'&&message.text.trim()).slice(-4).map(message=>message.text.trim());
  const context=[...recent, ...(direct?[direct]:[])].join('\n').slice(-2000);
  return context || assistantDescription.trim().slice(0,1900) || '根据当前会话上下文选择最合适的模型。';
};
export const loadChatBootstrap=async(conversationId:string)=>{
  const [messages,detail]=await Promise.all([
    api<Message[]>(`/conversations/${conversationId}/messages`),
    api<Conversation>(`/conversations/${conversationId}`),
  ]);
  const runId=detail.active_run_id||[...messages].reverse().find(message=>message.run_id)?.run_id;
  let run:Run|null=null;let runError:unknown=null;
  if(runId){
    try{run=await api<Run>(`/runs/${runId}`);}
    catch(error){runError=error;}
  }
  return {messages,detail,run,runError};
};
export const loadChatSkills=async(enabled:boolean,assistantId:string,signal?:AbortSignal):Promise<Skill[]>=>enabled?api<Skill[]>(`/assistants/${assistantId}/skills`,{signal}):[];
export const chatMessagePayload=(input:{requestId:string;text:string;fileIds:string[];skillIds:string[];trial:boolean;autoModelRouting:boolean})=>({
  client_request_id:input.requestId,
  text:input.text,
  file_ids:input.fileIds,
  skill_ids:input.skillIds,
  auto_route:input.trial?false:input.autoModelRouting,
});
export const retryMessagePayload=(message:Pick<Message,'text'|'file_ids'|'skills'>)=>({
  text:message.text.trim() || (message.file_ids.length?'请阅读我上传的附件。':message.skills?.length?'请执行所选 Skill。':''),
  fileIds:[...message.file_ids],
  skillIds:(message.skills||[]).map(skill=>skill.id),
});
export const sendChatMessage=(conversationId:string,payload:ReturnType<typeof chatMessagePayload>)=>post<{message:Message;run_id:string}>(`/conversations/${conversationId}/messages`,payload);
export const chatTrialFeatures=(trial:boolean, config?:AppConfig)=>({
  canUseAttachments:!trial,
  canUseSkills:!trial,
  // Tab is a bounded, low-risk request.  Controlled trials may expose it
  // only when the server advertises the explicit capability; normal mode
  // keeps the historical always-on behaviour.
  canUseCompletion:!trial || Boolean(config && configAllows(config,'models.complete')),
  canUseContext:!trial,
  canRecommend:!trial,
});

export default function ChatPage({ conversation, assistant, config, models, modelsLoading, initialDraft, onChanged, onArchived, onMemoryDirtyChange, onMemorySavingChange }: { conversation: Conversation; assistant?: Assistant; config: AppConfig; models: ModelOption[]; modelsLoading: boolean; initialDraft?: string; onChanged: () => void; onArchived: () => void; onMemoryDirtyChange?: (dirty:boolean)=>void; onMemorySavingChange?: (saving:boolean)=>void }) {
  const trial = config.deployment_mode === 'trial'; const canSend = configAllows(config, 'messages.send'); const {canUseAttachments,canUseSkills,canUseCompletion,canUseContext,canRecommend} = chatTrialFeatures(trial,config);
  const [messages, setMessages] = useState<Message[]>([]); const [run, setRun] = useState<Run | null>(null); const [events, setEvents] = useState<StreamEvent[]>([]);
  const [archived,setArchived]=useState(conversation.archived);const [restoring,setRestoring]=useState(false);
  const [showContext, setShowContext] = useState(false);
  const [skills, setSkills] = useState<Skill[]>([]); const [selectedSkills, setSelectedSkills] = useState<string[]>([]); const [skillQuery, setSkillQuery] = useState<string|null>(null);const [skillHighlight,setSkillHighlight]=useState(0); const mention = useRef<ReturnType<typeof mentionQuery>>(null); const [runStage,setRunStage]=useState('');
  useEffect(()=>{let current=true;loadChatSkills(canUseSkills,conversation.assistant_id).then(result=>{if(current)setSkills(result);}).catch(()=>{});return()=>{current=false;};},[conversation.assistant_id,canUseSkills]);
  const [selection, setSelection] = useState<ModelSelection>({ model_id: conversation.model_id, reasoning_level: conversation.reasoning_level });
  const [autoModelRouting, setAutoModelRouting] = useState(!trial && Boolean(conversation.auto_model_routing));
  const [recommendOpen, setRecommendOpen] = useState(false); const [recommendSummary, setRecommendSummary] = useState(''); const [recommendation, setRecommendation] = useState<ModelRecommendation | null>(null); const [recommendBusy, setRecommendBusy] = useState(false);
  const [savingModel, setSavingModel] = useState(false); const selectionVersion = useRef(0);
  const [hasNativeSession, setHasNativeSession] = useState(conversation.has_native_session);
  const [modelReconnectRequired, setModelReconnectRequired] = useState(false);
  const [refreshingRun, setRefreshingRun] = useState(false);
  const interactionStart=useRef<number|null>(null);
  const [localFeedback,setLocalFeedback]=useState<{ms:number;runId:string|null}|null>(null);
  const [loading, setLoading] = useState(true); const [error, setError] = useState(''); const [input, setInput] = useState(initialDraft || ''); const [sending, setSending] = useState(false); const [retrying, setRetrying] = useState(false); const [uploading, setUploading] = useState(false);
  useLayoutEffect(()=>{if(sending&&interactionStart.current!==null){setLocalFeedback({ms:Math.max(0,performance.now()-interactionStart.current),runId:null});}},[sending]);
  const [files, setFiles] = useState<StoredFile[]>([]); const [preview, setPreview] = useState<StoredFile | null>(null); const [title, setTitle] = useState(conversation.title); const [titleDraft, setTitleDraft] = useState(conversation.title); const titleVersion = useRef(0); const [rename, setRename] = useState(false); const [archive, setArchive] = useState(false); const [savingTitle, setSavingTitle] = useState(false); const [showFeed, setShowFeed] = useState(false); const [connectionState, setConnectionState] = useState<'connected' | 'reconnecting'>('connected'); const [stopping, setStopping] = useState(false); const [decisionBusy, setDecisionBusy] = useState('');
  const composerForm = useRef<HTMLFormElement>(null); const composing = useRef(false); const inputVersion = useRef(0);
  const submissionPending = useRef(false);
  const retryRequest = useRef<{ signature: string; id: string } | null>(null);
  const scroll = useRef<HTMLDivElement>(null); const shouldFollow = useRef(true); const [showScroll, setShowScroll] = useState(false); const fileInput = useRef<HTMLInputElement>(null); const textInput = useRef<HTMLTextAreaElement>(null); const request = useRef<{ signature: string; id: string } | null>(null); const mounted = useRef(true); const onChangedRef = useRef(onChanged); onChangedRef.current = onChanged;
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const applyRunReadback = useCallback((next: Run) => {
    if (!mounted.current) return;
    // HTTP snapshots can arrive after a terminal SSE event or a newer task.
    // A read refreshes the selected run; it cannot select an old run or revive it.
    setRun(current => !current || current.id !== next.id || (!isActive(current.status) && isActive(next.status)) ? current : next);
  }, []);
  const reloadMessages = useCallback(async () => { const next = await api<Message[]>(`/conversations/${conversation.id}/messages`); if (mounted.current) setMessages(next); return next; }, [conversation.id]);
  useEffect(() => {
    let current = true;
    const requestedTitleVersion = titleVersion.current;
    const requestedSelectionVersion = selectionVersion.current;
    const load = async () => {
      try {
        const {messages:next,detail,run:loadedRun,runError} = await loadChatBootstrap(conversation.id);
        if (!current) return;
        setMessages(next);
        setHasNativeSession(detail.has_native_session);setArchived(detail.archived);
        // A rename can finish before this initial GET. Its older snapshot must not undo it.
        if (titleVersion.current === requestedTitleVersion) setTitle(detail.title);
        if (selectionVersion.current === requestedSelectionVersion) { setSelection({ model_id: detail.model_id, reasoning_level: detail.reasoning_level }); setAutoModelRouting(!trial && Boolean(detail.auto_model_routing)); }
        if (loadedRun) setRun(loadedRun);
        else if (runError) setError(errorText(runError));
      } catch (e) { if (current) setError(errorText(e)); }
      finally { if (current) setLoading(false); }
    };
    void load();
    return () => { current = false; };
  }, [conversation.id]);
  useEffect(() => {
    if (!run?.id) return;
    const runId = run.id; const runTitleVersion=titleVersion.current; let current = true; let finalizing = false; const seen = new Set<number>(); setEvents([]); setConnectionState('connected');
    const source = new EventSource(`${API_BASE}/runs/${encodeURIComponent(runId)}/events?after=0`, { withCredentials: true });
    const finalize = async () => { if (finalizing || !current) return; finalizing = true; setRefreshingRun(true); source.close(); try { const [nextRun, , detail] = await Promise.all([api<Run>(`/runs/${runId}`), reloadMessages(), api<Conversation>(`/conversations/${conversation.id}`)]); if (current) { applyRunReadback(nextRun); if(titleVersion.current===runTitleVersion) setTitle(detail.title); setHasNativeSession(detail.has_native_session); setSelection({ model_id: detail.model_id, reasoning_level: detail.reasoning_level }); setAutoModelRouting(!trial && Boolean(detail.auto_model_routing)); setConnectionState('connected'); onChangedRef.current(); } } catch (e) { if (current) setError(errorText(e)); } finally { finalizing = false; if (current) setRefreshingRun(false); } };
    const receive = (raw: Event) => {
      if (!current) return;
      let event: StreamEvent;
      try { event = JSON.parse((raw as MessageEvent).data) as StreamEvent; } catch { setError('收到无法识别的执行事件，请刷新会话重新读取。'); return; }
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
      if (event.type === 'run.completed') { setRun(prev => prev ? { ...prev, status: event.data.status as RunStatus, error: typeof event.data.error === 'string' ? event.data.error : null, error_code: typeof event.data.error_code === 'string' ? event.data.error_code : null } : prev); void finalize(); }
    };
    eventTypes.forEach(type => source.addEventListener(type, receive));
    source.onopen = () => { if (current) setConnectionState('connected'); };
    source.onerror = () => { if (!current) return; setConnectionState('reconnecting'); void api<Run>(`/runs/${runId}`).then(next => { if (!current) return; if (!isActive(next.status)) void finalize(); else applyRunReadback(next); }).catch(() => {}); };
    return () => { current = false; source.close(); };
  // Only a different run starts a different event subscription.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run?.id, reloadMessages]);
  const historyResultIds = configAllows(config, 'runs.read') ? historicalResultMessages(messages, conversation.id, run?.id) : new Set<string>();
  const liveText = streamText(events); const active = !!run && isActive(run.status); const pendingIds = pendingApprovalIds(events);
  const aiWorking = run?.status === 'running' && run.execution_kind !== 'product_metadata' && connectionState === 'connected';
  const selectedModel = models.find(model => model.id === selection.model_id);
  const selectedReasoningLabel = selectedModel?.reasoning_levels.find(level => level.id === selection.reasoning_level)?.label;
  const modelReady = !!selectedModel?.available && !!selectedReasoningLabel && !modelsLoading;
  const hasHistory = conversationHasModelHistory(hasNativeSession, messages.length);
  const approvalsFromEvents = events.flatMap(e => {
    if (e.type === 'approval.required' && pendingIds.has(String(e.data.id))) return [e.data as unknown as Approval];
    if (e.type === 'subagent.approval.required' && e.data.approval && pendingIds.has(String(e.data.approval_id))) return [e.data.approval as Approval];
    return [];
  });
  const approvals = active ? [...new Map([...(run?.approvals || []).filter(a => a.status === 'pending'), ...approvalsFromEvents].map(a => [a.id, a])).values()].filter(a => !events.some(e => e.type === 'approval.resolved' && e.data.approval_id === a.id)) : [];
  const canonicalHasAnswer = messages.some(m => m.role === 'assistant' && m.run_id === run?.id && !!m.text);
  const liveCitations=events.filter(e=>e.type==='message.updated').at(-1)?.data.citations as Message['citations'];
  const showStream = !!run && (active || !canonicalHasAnswer) && !!liveText;
  const artifacts = events.filter(e => e.type === 'artifact.created').map(e => e.data as unknown as StoredFile);
  useEffect(() => { if (shouldFollow.current && scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight; }, [messages, liveText, loading, events.length]);
  useLayoutEffect(() => {
    const node = textInput.current; if (!node) return;
    const resize = () => {
      const maxHeight = composerInputMaxHeight(window.visualViewport?.height || window.innerHeight);
      node.style.maxHeight = `${maxHeight}px`; node.style.height = 'auto';
      node.style.height = `${Math.min(node.scrollHeight, maxHeight)}px`;
    };
    resize(); let width = node.clientWidth;
    const observer = new ResizeObserver(() => { if (node.clientWidth !== width) { width = node.clientWidth; resize(); } });
    observer.observe(node); window.addEventListener('resize', resize); window.visualViewport?.addEventListener('resize', resize);
    return () => { observer.disconnect(); window.removeEventListener('resize', resize); window.visualViewport?.removeEventListener('resize', resize); };
  }, [input]);
  const matchingSkills=skills.filter(skill=>skill.enabled&&!selectedSkills.includes(skill.id)&&(skill.name+' '+skill.description).toLowerCase().includes((skillQuery||'').toLowerCase()));
  useEffect(()=>{setSkillHighlight(0);},[skillQuery]);
  const closeModelSettings = () => { const details = composerForm.current?.querySelector<HTMLDetailsElement>('.chat-model-settings'); if (details) details.open = false; };
  const fillDraft = (value: string) => {
    inputVersion.current += 1; const version = inputVersion.current; setInput(value); setSkillQuery(null); mention.current = null;
    textInput.current?.focus();
    requestAnimationFrame(() => { const node = textInput.current; if (mounted.current && inputVersion.current === version && node && document.activeElement === node) node.setSelectionRange(value.length, value.length); });
  };
  const assistance = useComposerAssistance({ conversationId: conversation.id, draft: input, hasExtraDraft: files.length > 0 || selectedSkills.length > 0,
    completionEnabled: canUseCompletion, followupsEnabled: configAllows(config, 'models.followups'),
    disabled: archived || sending || loading || savingModel || showContext || !canSend,
    settled: !loading && !refreshingRun && !sending, run, messages, onDraft: fillDraft });
  const changeDraft = (value: string, caret: number) => {
    assistance.invalidate(); inputVersion.current += 1; setInput(value);
    if (canUseSkills) { mention.current = mentionQuery(value, caret); setSkillQuery(mention.current?.query ?? null); if (mention.current) closeModelSettings(); }
    else { mention.current = null; setSkillQuery(null); }
  };
  const addSkill = (skill: Skill) => {
    if (selectedSkills.length >= 5) return;
    assistance.invalidate(); inputVersion.current += 1; setSelectedSkills(prev => [...new Set([...prev, skill.id])].slice(0, 5));
    if (mention.current) { const { start, end } = mention.current; setInput(prev => prev.slice(0, start) + prev.slice(end)); }
    setSkillQuery(null); mention.current = null; textInput.current?.focus();
  };
  const send = async () => {
    if (!canSend || (!input.trim() && files.length === 0 && selectedSkills.length===0) || archived || sending || submissionPending.current || uploading || loading || refreshingRun || savingModel || showContext || !modelReady) return;
    const text = input.trim() || (files.length?'请阅读我上传的附件。':'请执行所选 Skill。'); const fileIds = files.map(f => f.id); const skillIds = selectedSkillIds(selectedSkills,skills); const signature = JSON.stringify([text, fileIds,skillIds]);
    if (!request.current || request.current.signature !== signature) request.current = { signature, id: clientRequestId() };
    const sentInputVersion = inputVersion.current; assistance.invalidate();
    submissionPending.current = true;
    interactionStart.current=performance.now(); setSending(true); setError(''); shouldFollow.current = true;
    try {
      if (active || queue.state?.items.length) {
        await queue.enqueue({ ...chatMessagePayload({requestId:request.current.id,text,fileIds,skillIds,trial,autoModelRouting}), model_id: selection.model_id, reasoning_level: selection.reasoning_level });
        if (!mounted.current) return;
        if (inputVersion.current === sentInputVersion) setInput(''); setFiles([]); setSelectedSkills([]); setSkillQuery(null); request.current = null; return;
      }
      const result = await sendChatMessage(conversation.id,chatMessagePayload({requestId:request.current.id,text,fileIds,skillIds,trial,autoModelRouting}));
      if (!mounted.current) return;
      setLocalFeedback(previous=>previous?{...previous,runId:result.run_id}:previous);
      setMessages(previous => previous.some(m => m.id === result.message.id) ? previous : [...previous, result.message]); if (inputVersion.current === sentInputVersion) setInput(''); setFiles([]); setSelectedSkills([]); setSkillQuery(null);setRunStage('initializing'); request.current = null;
      setRun({ id: result.run_id, conversation_id: conversation.id, status: 'queued', assistant_message_id: null, error: null, usage: null, approvals: [], created_at: new Date().toISOString(), completed_at: null });
      const next = await api<Run>(`/runs/${result.run_id}`); if (!mounted.current) return; applyRunReadback(next); onChangedRef.current();
    } catch (e) { if (mounted.current) { setError(errorText(e)); if (e instanceof ApiError && e.code === 'MODEL_SELECTION_REQUIRED') setModelReconnectRequired(true); } } finally { submissionPending.current = false; if (mounted.current) setSending(false); }
  };
  const queueReadVersion = useRef(0);
  const queue = useMessageQueue(conversation.id, canSend && !loading && !active && !sending && !savingModel && !showContext && !archived && !refreshingRun && modelReady, `${run?.id || ''}:${run?.status || ''}`, async runId => {
    const version = ++queueReadVersion.current;
    // Select an accepted task immediately. Later HTTP reads use the same
    // terminal-state guard as normal sends and cannot revive a finished run.
    setRun(current => current?.id === runId || current && isActive(current.status) ? current : { id: runId, conversation_id: conversation.id, status: 'queued', assistant_message_id: null, error: null, usage: null, approvals: [], created_at: new Date().toISOString(), completed_at: null });
    const [next, nextMessages] = await Promise.all([api<Run>(`/runs/${runId}`), api<Message[]>(`/conversations/${conversation.id}/messages`)]);
    if (!mounted.current || version !== queueReadVersion.current) return;
    setMessages(nextMessages); applyRunReadback(next); onChangedRef.current();
  });
  const [showMemory, setShowMemory] = useState(false), [memoryDirty, setMemoryDirty] = useState(false), [memorySaving, setMemorySaving] = useState(false), [confirmCloseMemory, setConfirmCloseMemory] = useState(false);
  const memoryDirtyChanged = useCallback((dirty:boolean) => { setMemoryDirty(dirty); onMemoryDirtyChange?.(dirty); }, [onMemoryDirtyChange]);
  const memorySavingChanged = useCallback((saving:boolean) => { setMemorySaving(saving); onMemorySavingChange?.(saving); }, [onMemorySavingChange]);
  const closeMemory = () => { if (memorySaving) return; if (memoryDirty) setConfirmCloseMemory(true); else setShowMemory(false); };
  const retryLastMessage = async () => {
    if (!canSend || archived || active || sending || submissionPending.current || uploading || loading || refreshingRun || savingModel || showContext || !modelReady) return;
    const lastUserMessage = [...messages].reverse().find(message => message.role === 'user');
    if (!lastUserMessage) { setError('找不到可重试的用户消息，请重新输入任务。'); return; }
    const { text, fileIds, skillIds } = retryMessagePayload(lastUserMessage);
    if (!text) { setError('上一条任务没有可重试的内容，请重新输入任务。'); return; }
    if (fileIds.length && !canUseAttachments) { setError('当前部署不支持重试原任务附件，请重新发送文字任务。'); return; }
    if (skillIds.length && !canUseSkills) { setError('当前部署不支持重试原任务 Skill，请重新发送文字任务。'); return; }
    // A deliberate retry starts a new task. If its response is lost, repeating
    // that same retry must resolve the pending request, not execute tools again.
    const signature = JSON.stringify([run?.id, lastUserMessage.id, text, fileIds, skillIds]);
    if (!retryRequest.current || retryRequest.current.signature !== signature) retryRequest.current = { signature, id: clientRequestId() };
    submissionPending.current = true;
    // Keep the previous result and its CSV available until a new run is accepted.
    // The run subscription resets events only after the run ID changes.
    assistance.invalidate(); setRetrying(true); setSending(true); setError(''); shouldFollow.current = true;
    try {
      const result = await sendChatMessage(conversation.id, chatMessagePayload({ requestId: retryRequest.current.id, text, fileIds, skillIds, trial, autoModelRouting }));
      if (!mounted.current) return;
      setMessages(previous => previous.some(message => message.id === result.message.id) ? previous : [...previous, result.message]);
      retryRequest.current = null; setRunStage('initializing');
      setRun({ id: result.run_id, conversation_id: conversation.id, status: 'queued', assistant_message_id: null, error: null, usage: null, approvals: [], created_at: new Date().toISOString(), completed_at: null });
      const next = await api<Run>(`/runs/${result.run_id}`); if (!mounted.current) return; applyRunReadback(next); onChangedRef.current();
    } catch (e) { if (mounted.current) { setError(errorText(e)); if (e instanceof ApiError && e.code === 'MODEL_SELECTION_REQUIRED') setModelReconnectRequired(true); } }
    finally { submissionPending.current = false; if (mounted.current) { setRetrying(false); setSending(false); } }
  };
  const upload = async (selectedFiles: FileList | null) => {
    if (!canUseAttachments || !selectedFiles || uploading) return;
    const selected = Array.from(selectedFiles);
    const problem = chatAttachmentProblem([...files, ...selected], conversation.runtime_type || 'claude', config.upload_max_bytes);
    if (problem) { setError(problem); if (fileInput.current) fileInput.current.value = ''; return; }
    assistance.invalidate(); setUploading(true); setError('');
    try {
      for (const file of selected) {
        const body = new FormData(); body.append('file', file);
        const saved = await api<StoredFile>('/files', { method: 'POST', body });
        if (mounted.current) setFiles(prev => [...prev, saved]);
      }
    } catch (e) { if (mounted.current) setError(errorText(e)); }
    finally { if (mounted.current) setUploading(false); if (fileInput.current) fileInput.current.value = ''; }
  };
  const decide = async (approval: Approval, decision: 'allow' | 'deny', memoryDigest?: string):Promise<boolean> => {
    setDecisionBusy(approval.id); setError('');
    try {
      await post(`/approvals/${approval.id}/decision`, { decision, ...(memoryDigest?{memory_content_digest:memoryDigest}:{}) });
    } catch (e) { setError(errorText(e)); setDecisionBusy(''); return false; }
    // The approval is already recorded. A refresh failure must not invite a duplicate decision.
    if (run) { try { applyRunReadback(await api<Run>(`/runs/${run.id}`)); } catch (e) { setError(`审批已提交，但执行状态暂未刷新：${errorText(e)}`); } }
    setDecisionBusy(''); return true;
  };
  const stop = async () => { if (!run) return; setStopping(true); try { await post(`/runs/${run.id}/cancel`); applyRunReadback(await api<Run>(`/runs/${run.id}`)); } catch (e) { setError(errorText(e)); } finally { setStopping(false); } };
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
  const changeModel = async (next: ModelSelection, reconnect = false): Promise<boolean> => {
    if (active || loading || sending || savingModel || showContext || uploading || refreshingRun) return false;
    if (!reconnect && !autoModelRouting && next.model_id === selection.model_id && next.reasoning_level === selection.reasoning_level) return true;
    if (!models.some(model => model.id === next.model_id && model.available && model.reasoning_levels.some(level => level.id === next.reasoning_level))) return false;
    if (modelSwitchRequiresNewConversation(models, selection.model_id, next.model_id, hasHistory)) {
      setError('请新建会话使用此模型，当前会话和记忆保留。'); return false;
    }
    selectionVersion.current += 1; setSavingModel(true); setError('');
    try {
      const saved = await saveModelSelection(conversation.id, selection, next, reconnect || autoModelRouting);
      if (!mounted.current || !saved) return false;
      // The successful server response is authoritative; a rejected change keeps the current selection.
      setSelection({ model_id: saved.model_id, reasoning_level: saved.reasoning_level });
      setAutoModelRouting(!trial && Boolean(saved.auto_model_routing));
      setHasNativeSession(saved.has_native_session);
      setModelReconnectRequired(false); setError('');
      request.current = null;
      onChangedRef.current();
      return true;
    } catch (e) { if (mounted.current) { setError(errorText(e)); if (e instanceof ApiError && e.code === 'MODEL_SELECTION_REQUIRED') setModelReconnectRequired(true); } return false; }
    finally { if (mounted.current) setSavingModel(false); }
  };
  const recommend = async (summaryOverride?: string) => {
    if (!canRecommend) return;
    const summary = (summaryOverride ?? recommendSummary).trim();
    if (!summary || recommendBusy || active) { if (!summary && mounted.current) setError('当前会话没有足够上下文，暂时无法推荐模型。'); return; }
    setRecommendBusy(true); setError(''); setRecommendSummary(summary);
    try {
      const next = await post<ModelRecommendation>('/models/recommend', { task_summary: summary, candidate_model_ids: models.filter(model => model.available).map(model => model.id) });
      if (!mounted.current) return;
      setRecommendation(next);
      // The operation is deliberately explicit (the user clicked the button),
      // then applies the bounded recommendation without a second confirmation.
      const applied = await changeModel({ model_id: next.model_id, reasoning_level: next.reasoning_level });
      if (!applied) return;
      // Applying a recommendation fixes this selection; automatic routing is
      // enabled only through the visible mode control.
    }
    catch (e) { if (mounted.current) setError(errorText(e)); }
    finally { if (mounted.current) setRecommendBusy(false); }
  };
  const changeRouting = async (automatic: boolean) => {
    if (trial || automatic === autoModelRouting || active || loading || sending || savingModel || showContext || uploading || refreshingRun || recommendBusy) return;
    selectionVersion.current += 1; setSavingModel(true); setError('');
    try {
      const saved = await saveModelRouting(conversation.id, automatic);
      if (!mounted.current) return;
      setAutoModelRouting(Boolean(saved.auto_model_routing));
      setSelection({ model_id: saved.model_id, reasoning_level: saved.reasoning_level });
      request.current = null; onChangedRef.current();
    } catch (error) { if (mounted.current) setError(errorText(error)); }
    finally { if (mounted.current) setSavingModel(false); }
  };
  const openRecommendation = () => { const summary = fitnessTaskSummary(input,messages,assistant?.description||''); setRecommendSummary(summary); setRecommendation(null); setRecommendOpen(true); void recommend(summary); };
  return <div className="chat-page"><header className="chat-header"><div className="chat-heading"><AiIdentity active={aiWorking}/><div><h1>{title || '新会话'}</h1><span>{assistant?.name || '业务助手'}{active && <><i/> {statusLabels[run.status]}</>}</span></div></div><div className="chat-actions">{!trial&&<button type="button" className="icon-button" title="会话记忆" aria-label="会话记忆" onClick={()=>setShowMemory(true)}><BookOpen size={17}/></button>}<button className="icon-button" title="重命名会话" aria-label="重命名会话" onClick={() => { setTitleDraft(title); setRename(true); }}><Pencil size={16}/></button>{canUseContext&&<button type="button" className="icon-button" title="上下文" aria-label="上下文" disabled={savingModel || loading} onClick={() => setShowContext(true)}><Gauge size={17}/></button>}<button className="icon-button" title="归档会话" aria-label="归档会话" disabled={active} onClick={() => setArchive(true)}><Archive size={17}/></button><button aria-label="执行记录" aria-expanded={showFeed} className={`feed-toggle ${showFeed ? 'selected' : ''}`} onClick={() => setShowFeed(!showFeed)}><Radio size={16}/><span>执行记录</span>{active && <span className="tiny-dot"/>}</button></div></header>
    <div className="chat-proof-bar" aria-label="会话能力状态"><span><FileText size={14}/>业务会话</span><span>{assistant?.connections.length||0} 个已配置 MCP</span><span className="run-status-pill" data-state={run?.status || 'idle'}>{run?.status === 'succeeded' ? <CheckCircle2 size={14}/> : run?.status === 'failed' ? <XCircle size={14}/> : run?.status === 'awaiting_approval' ? <ShieldCheck size={14}/> : <Clock3 size={14}/>} {run?statusLabels[run.status]:'等待开始'}</span>{run&&<RunModelIdentity run={run} models={models}/>}{connectionState === 'reconnecting' && <strong>连接恢复中</strong>}</div>
    <div className="chat-layout"><section className="conversation-column"><div className="message-scroll" ref={scroll} onScroll={() => { const el = scroll.current; if (!el) return; shouldFollow.current = el.scrollHeight - el.scrollTop - el.clientHeight < 120; setShowScroll(!shouldFollow.current); }}>
      {loading ? <Loader text="正在恢复会话…"/> : messages.length === 0 && !liveText ? <div className="chat-intro"><AiIdentity size="large"/><h2>向{assistant?.name || '业务助手'}提问</h2><p>{assistant?.description || '描述你的问题或目标，也可以上传相关资料。'}</p><div className="chat-intro-note"><ShieldCheck size={16}/>调用需审批的业务工具时，你会先看到操作内容。</div></div> : <div className="messages">{messages.filter(m => !(active && m.role === 'assistant' && m.run_id === run?.id) && (m.role !== 'assistant' || !!m.text || m.file_ids.length > 0 || historyResultIds.has(m.id))).map(message => <article className={`message ${message.role}`} key={message.id}><div className="message-label">{message.role === 'assistant' ? <><AiIdentity size="inline"/>{assistant?.name || '助手'}</> : <>你<span>{timeText(message.created_at)}</span></>}</div><div className="message-content">{message.role === 'assistant' ? <AnswerMarkdown text={message.text} citations={message.citations}/> : <p className="user-text">{run?.kind === 'compact' && message.run_id === run.id ? '请求原生上下文压缩' : message.text}</p>}{message.role==='user'&&!!message.skills?.length&&<div className="message-skill-record">{message.skills.map(skill=><span key={skill.id}>@{skill.name} · v{skill.revision}</span>)}</div>}{canUseAttachments && message.file_ids.length > 0 && <div className="attachment-row">{message.file_ids.map(id => <Attachment key={id} id={id} onPreview={setPreview}/>)}</div>}{message.run_id && historyResultIds.has(message.id) && <HistoricalBusinessResults conversationId={conversation.id} runId={message.run_id} messageId={message.id} question={questionForRun(messages,message.run_id)}/>}</div></article>)}{showStream && <article className="message assistant"><div className="message-label"><AiIdentity size="inline"/>{assistant?.name || '助手'}{active && <span className="typing-indicator"><i/><i/><i/></span>}</div><div className="message-content"><AnswerMarkdown text={liveText} citations={liveCitations}/></div></article>}{run&&<BusinessResults events={events} runId={run.id} status={run.status} question={questionForRun(messages,run.id)}/>}{active && <LiveActivity run={run} events={events} stage={runStage} connectionState={connectionState}/>}<div className="compaction-message-container">{run?.kind === 'compact' && <div className="compaction-message"><Minimize2 size={17}/><div><strong>{compactionLabel(run)}</strong><p>历史消息与附件仍保留，可在“上下文”中查看原生统计。</p></div></div>}</div>{run && !active && ['failed', 'interrupted', 'cancelled'].includes(run.status) && <div className="run-outcome"><span className={`badge ${run.status === 'failed' ? 'amber' : ''}`}>{statusLabels[run.status]}</span><p>{run.error || (run.status === 'interrupted' ? '服务重启或连接中断。你可以继续发送消息恢复会话，之前的业务操作不会自动重做。' : '你可以继续发送消息。已执行的业务操作不会自动撤销。')}</p><div className="run-outcome-actions">{run.error_code === 'MCP_RESULT_TOO_LARGE' ? <><Button variant="secondary" onClick={() => textInput.current?.focus()}><Pencil size={15}/>调整查询条件</Button><small>先缩小查询范围，或请业务系统提供聚合结果；直接重复原请求仍可能超限。</small></> : <><Button variant="secondary" busy={retrying} disabled={retrying || sending || active || refreshingRun} onClick={() => void retryLastMessage()}><RefreshCw size={15}/>重试本次任务</Button><small>使用原问题、附件和 Skill 重新执行。业务工具可能再次被调用。</small></>}</div></div>}{!trial && artifacts.length > 0 && <div className="artifact-section"><span className="eyebrow">生成的文件</span><div className="attachment-row">{artifacts.map(file => <FileChip key={file.id} file={file} onPreview={() => setPreview(file)}/>)}</div></div>}</div>}
    </div>{showScroll && <button className="scroll-latest" onClick={() => { if (scroll.current) scroll.current.scrollTo({ top: scroll.current.scrollHeight, behavior: 'smooth' }); shouldFollow.current = true; }}><ArrowDown size={15}/>回到最新消息</button>}
    <div className="composer-area composer-refined">{archived&&<div className="archive-banner"><span>此会话已归档，恢复后可以继续工作。</span><Button variant="secondary" busy={restoring} onClick={async()=>{setRestoring(true);try{await patch(`/conversations/${conversation.id}`,{archived:false});setArchived(false);onChangedRef.current();}catch(e){setError(errorText(e));}finally{setRestoring(false);}}}>恢复会话</Button></div>}<ErrorNotice message={error} onDismiss={() => setError('')}/>{modelReconnectRequired && <ModelRecovery currentAvailable={modelReady} busy={active || loading || sending || savingModel || uploading || showContext || refreshingRun} pending={savingModel} onReconnect={() => void changeModel(selection, true)}/>}{approvals.length > 0 && !showFeed && <button className="approval-banner" onClick={() => setShowFeed(true)}><ShieldCheck size={17}/>有 {approvals.length} 项操作等待你的确认<ArrowUp size={16}/></button>}
      <MessageQueue queue={queue}/>
      <ComposerFollowups key={assistance.followups.runId || 'pending'} value={assistance.followups} onChoose={assistance.chooseFollowup}/>
      <form ref={composerForm} className="composer fw-composer" data-working={active || sending || assistance.completion.busy || undefined} onSubmit={e => { e.preventDefault(); void send(); }} onClickCapture={e => { if ((e.target as Element).closest('.chat-model-settings > summary')) { setSkillQuery(null); mention.current = null; } }}>
        {canUseSkills && skillQuery !== null && <SkillPicker skills={skills} selected={selectedSkills} query={skillQuery} highlighted={skillHighlight} onSelect={addSkill} onClose={() => { setSkillQuery(null); mention.current = null; textInput.current?.focus(); }}/>}
        {canUseSkills && selectedSkills.length > 0 && <SkillChips selected={selectedSkills} skills={skills} onRemove={id => { assistance.invalidate(); setSelectedSkills(prev => prev.filter(item => item !== id)); }} disabled={sending}/>}
        {canUseAttachments && files.length > 0 && <div className="attachment-row composer-files">{files.map(file => <FileChip key={file.id} file={file} onPreview={() => setPreview(file)} onRemove={() => { assistance.invalidate(); setFiles(prev => prev.filter(f => f.id !== file.id)); }}/>)}</div>}
        <textarea ref={textInput} aria-label="输入消息" aria-describedby="composer-shortcuts" placeholder={active ? '输入下一条问题，加入队列…' : '描述你的问题或工作…'} value={input}
          onChange={e => changeDraft(e.target.value, e.target.selectionStart)}
          onCompositionStart={() => { composing.current = true; assistance.invalidate(); }} onCompositionEnd={() => { composing.current = false; }}
          onKeyDown={e => {
            const inComposition = composing.current || e.nativeEvent.isComposing || e.nativeEvent.keyCode === 229;
            if (inComposition) return;
            if (skillQuery !== null && ['ArrowDown', 'ArrowUp', 'Enter'].includes(e.key)) {
              e.preventDefault();
              if (e.key === 'Enter') { const choice = matchingSkills[skillHighlight]; if (choice) addSkill(choice); else setSkillQuery(null); }
              else if (matchingSkills.length) setSkillHighlight(previous => (previous + (e.key === 'ArrowDown' ? 1 : -1) + matchingSkills.length) % matchingSkills.length);
              return;
            }
            if (e.key === 'Escape') {
              if (skillQuery !== null) { e.preventDefault(); setSkillQuery(null); mention.current = null; }
              else if (assistance.completion.busy || assistance.completion.suggestion || assistance.completion.notice) { e.preventDefault(); assistance.invalidate(); }
              return;
            }
            const action = composerShortcut({ key: e.key, shiftKey: e.shiftKey, ctrlKey: e.ctrlKey, metaKey: e.metaKey, altKey: e.altKey, repeat: e.repeat, isComposing: e.nativeEvent.isComposing, keyCode: e.nativeEvent.keyCode }, { composing: composing.current, draft: input, canComplete: canUseCompletion && !archived && !sending && !loading && !savingModel && !showContext && canSend, completionBusy: assistance.completion.busy, suggestion: !!assistance.completion.suggestion, skillsOpen: skillQuery !== null });
            if (action !== 'none') { e.preventDefault(); if (action === 'complete') void assistance.complete(); else if (action === 'accept') assistance.accept(); else void send(); }
          }} rows={1}/>
        <ComposerCompletion value={assistance.completion} onAccept={assistance.accept} onDismiss={assistance.invalidate}/>
        <div className="composer-toolbar composer-actions">
          <div className="composer-utilities">
            {canUseAttachments && <><input ref={fileInput} type="file" multiple hidden accept={CHAT_ATTACHMENT_ACCEPT} onChange={e => void upload(e.target.files)}/><button className="attach-button composer-icon" type="button" aria-label={uploading ? '正在上传附件' : '添加附件'} title="添加附件" disabled={uploading || sending || loading || archived} onClick={() => fileInput.current?.click()}>{uploading ? <LoaderCircle className="spin" size={18}/> : <Paperclip size={18}/>}</button></>}
            {canUseSkills && <button type="button" className="attach-button composer-icon" aria-label="选择 Skill" title="选择 Skill" aria-expanded={skillQuery !== null} disabled={sending || loading || archived} onClick={() => { closeModelSettings(); mention.current = null; setSkillQuery(previous => previous === null ? '' : null); }}><Sparkles size={17}/></button>}
          </div>
          <ModelSettingsDisclosure automatic={autoModelRouting} selection={selection} models={models} loading={loading||modelsLoading} pending={savingModel}>{!trial&&<ModelRoutingControl automatic={autoModelRouting} disabled={archived||active||loading||sending||uploading||showContext||refreshingRun||savingModel||recommendBusy} onChange={value=>void changeRouting(value)}/>}{!autoModelRouting?<ModelSelector models={models} value={selection} hasHistory={hasHistory} loading={modelsLoading} disabled={archived || active || loading || sending || uploading || showContext || refreshingRun} pending={savingModel} onRecommend={canRecommend ? openRecommendation : undefined} recommendBusy={recommendBusy} onChange={next => void changeModel(next)}/>:<div className="model-auto-actions">{canRecommend&&<Button variant="ghost" disabled={archived||active||loading||sending||uploading||showContext||refreshingRun||savingModel} busy={recommendBusy} onClick={openRecommendation}><Sparkles size={14}/>为当前任务推荐模型</Button>}</div>}</ModelSettingsDisclosure>
          {canUseCompletion&&<span className="composer-input-hint"><kbd>Tab</kbd> 补全</span>}<span className="composer-action-space"/>
          {active && <Button type="button" variant="secondary" busy={stopping} onClick={() => void stop()} disabled={run.status === 'cancelling'}><Square size={13}/>{run.status === 'cancelling' ? '停止中' : '停止'}</Button>}
          <button className="send-button" type="submit" aria-label={active || queue.state?.items.length ? '加入消息队列' : '发送消息'} title={active || queue.state?.items.length ? '加入消息队列' : '发送消息'} disabled={!canSend || archived || sending || uploading || loading || refreshingRun || savingModel || showContext || !modelReady || (!input.trim() && !files.length && !selectedSkills.length)}>{sending ? <LoaderCircle size={18} className="spin"/> : <ArrowUp size={20}/>}</button>
        </div>
      </form>
      <div className="composer-footnote"><span id="composer-shortcuts"><kbd>Enter</kbd> 发送 · <kbd>Shift + Enter</kbd> 换行</span><span>重要信息请核对来源</span></div>
    </div></section>
    {showMemory && <Modal title="会话记忆" wide onClose={closeMemory}><div className="modal-body conversation-memory-panel"><MemoryWorkspace conversation={conversation} onDirtyChange={memoryDirtyChanged} onSavingChange={memorySavingChanged}/></div></Modal>}
    {confirmCloseMemory && <Modal title="放弃未保存的记忆修改" onClose={()=>setConfirmCloseMemory(false)}><div className="modal-body"><p>未保存的草稿将丢失。已经发送的修改仍会继续处理。</p></div><footer className="modal-footer"><Button variant="secondary" onClick={()=>setConfirmCloseMemory(false)}>继续编辑</Button><Button variant="danger" disabled={memorySaving} onClick={()=>{setConfirmCloseMemory(false);setShowMemory(false);}}>放弃修改并关闭</Button></footer></Modal>}
    {showFeed && <aside className="execution-panel"><header><div><Radio size={16}/><h2>执行记录</h2></div><button className="icon-button" aria-label="关闭执行记录" onClick={() => setShowFeed(false)}><X size={17}/></button></header><div className="execution-body"><div className="execution-runtime"><span>运行引擎</span><RuntimeBadge runtime={conversation.runtime_type}/></div>{run ? <><div className="execution-state"><span className={`badge ${run.status === 'succeeded' ? 'green' : run.status === 'failed' || run.status === 'awaiting_approval' ? 'amber' : ''}`}>{active && <LoaderCircle size={12} className="spin"/>}{statusLabels[run.status]}</span>{connectionState === 'reconnecting' && active && <small>连接断开，正在自动重连…</small>}</div><RunMetrics run={run}/>{localFeedback?.runId===run.id&&<p className="br-local-feedback">操作反馈：{localFeedback.ms.toFixed(0)} ms（浏览器）<br/>浏览器反馈耗时，不代表业务完成时间。</p>}<ExecutionEvidenceCard events={events} run={run}/>{approvals.map(approval => approval.tool_name===MEMORY_WRITE_TOOL?<MemoryApprovalCard key={approval.id} conversationId={conversation.id} approval={approval} busy={decisionBusy===approval.id} onDecision={(decision,digest)=>decide(approval,decision,digest)}/>:<ApprovalCard key={approval.id} approval={approval} busy={decisionBusy === approval.id} onDecision={decision => void decide(approval, decision)}/>)}<div className="event-list">{events.filter(e => ['run.started', 'run.stage', 'tool.started', 'tool.completed', 'approval.required', 'approval.resolved', 'approval.model_decided', 'approval.dual_vote', 'memory.pending', 'memory.updated', 'memory.aborted', 'subagent.plan.created', 'subagent.started', 'subagent.progress', 'subagent.completed', 'subagent.failed', 'subagent.cancelled', 'subagent.plan.completed', 'subagent.approval.required', 'subagent.approval.denied', 'subagent.approval.resolved', 'artifact.created', 'context.compaction_started', 'context.compacted', 'context.compaction_result', 'run.completed'].includes(e.type)).map(event => <EventRow key={event.id} event={event}/>)}</div>{!events.length && <p className="section-help">正在读取执行记录…</p>}</> : <div className="execution-empty"><Clock3 size={26}/><h3>本次执行记录</h3><p>发送消息后，这里会展示工具调用、待确认操作和执行结果。</p></div>}</div></aside>}
    </div>{!trial && showContext && <ContextDialog conversationId={conversation.id} models={models} busy={active || sending || uploading || savingModel || loading} run={run} onClose={() => setShowContext(false)} onCompact={result => { setShowContext(false); setShowFeed(true); setMessages(previous => previous.some(message => message.id === result.message.id) ? previous : [...previous, result.message]); setRun({ id: result.run_id, conversation_id: conversation.id, status: 'queued', kind: 'compact', compaction: null, assistant_message_id: null, error: null, usage: null, approvals: [], created_at: new Date().toISOString(), completed_at: null }); void api<Run>(`/runs/${result.run_id}`).then(applyRunReadback).catch(e => { if (mounted.current) setError(errorText(e)); }); onChangedRef.current(); }}/>} {preview && <FilePreview key={preview.id} file={preview} onClose={() => setPreview(null)}/>}{rename && <Modal title="重命名会话" onClose={() => { if (!savingTitle) setRename(false); }}><form onSubmit={e => { e.preventDefault(); void saveTitle(); }}><div className="modal-body"><Field label="会话名称"><input value={titleDraft} onChange={e => setTitleDraft(e.target.value)} disabled={savingTitle} maxLength={200} required autoFocus/></Field></div><footer className="modal-footer"><Button type="button" variant="secondary" disabled={savingTitle} onClick={() => setRename(false)}>取消</Button><Button busy={savingTitle} disabled={!titleDraft.trim()} type="submit">保存名称</Button></footer></form></Modal>}{archive && <Modal title="归档此会话" onClose={() => setArchive(false)}><div className="modal-body"><p>归档后，会话会从最近列表中移除。会话和附件仍保存在服务端。</p></div><footer className="modal-footer"><Button variant="secondary" onClick={() => setArchive(false)}>取消</Button><Button onClick={async () => { try { await patch(`/conversations/${conversation.id}`, { archived: true }); onChangedRef.current(); onArchived(); } catch (e) { setError(errorText(e)); setArchive(false); } }}>归档会话</Button></footer></Modal>}{recommendOpen && <Modal title="为当前任务推荐模型" onClose={() => { if (!recommendBusy) setRecommendOpen(false); }}><div className="modal-body"><p>根据当前问题、最近消息和助手职责推荐模型，只使用任务摘要。推荐生效后将固定使用该模型，可随时改回自动选择。</p><div className="fitness-context" role="status"><span className="eyebrow">当前会话上下文</span><p>{recommendSummary}</p></div>{recommendation && <div className="recommendation-result"><strong>推荐模型：{recommendation.model_label} · {recommendation.reasoning_level}</strong><p>{recommendation.reason}</p><small>路由器：{recommendation.router.model_id} · 输入 {recommendation.router.input_chars} 字符 · {recommendation.router.mode === 'provider' || recommendation.router.mode === 'provider_fallback' ? '已使用授权模型路由' : recommendation.router.target_status === 'unavailable' ? `目标线路不可用（${recommendation.router.target_model_id}）` : '本地备用规则'}</small></div>}</div><footer className="modal-footer"><Button type="button" variant="secondary" disabled={recommendBusy} onClick={() => setRecommendOpen(false)}>{recommendation ? '完成' : '关闭'}</Button></footer></Modal>}</div>;
}

export function LiveActivity({ run, events, stage, connectionState }: { run: Run; events: StreamEvent[]; stage: string; connectionState: 'connected' | 'reconnecting' }) {
  const toolFinished = new Set(events.filter(event => event.type === 'tool.completed').map(event => String(event.data.tool_call_id || event.id)));
  const pendingTool = [...events].reverse().find(event => event.type === 'tool.started' && !toolFinished.has(String(event.data.tool_call_id || event.id)));
  const latestTool = [...events].reverse().find(event => event.type === 'tool.completed');
  const latestStage = [...events].reverse().find(event => event.type === 'run.stage' && event.data.stage === stage);
  const latestApproval = [...events].reverse().find(event => event.type === 'approval.required' || event.type === 'subagent.approval.required');
  const latestCompaction = [...events].reverse().find(event => event.type.startsWith('context.compaction'));
  let title = '正在处理当前任务';
  let detail = '可在执行记录中查看处理进度。';
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
    title = '已请求停止'; detail = '正在确认可停止的步骤；已发出的远端操作可能仍在执行。'; icon = <Square size={15}/>;
  } else if (stage === 'model_retry' && latestStage?.data.stage === 'model_retry') {
    title = '模型连接中断，正在重试'; detail = '已取得的业务数据会保留，你仍可以停止本次任务。';
  } else if (latestCompaction && latestCompaction.type === 'context.compaction_started') {
    title = '正在整理上下文'; detail = '原生上下文压缩已开始，完成后会继续当前任务。'; icon = <Minimize2 size={16}/>;
  } else if (pendingTool) {
    title = `正在调用 ${toolLabel(String(pendingTool.data.tool_name || '业务工具'))}`;
    detail = '已记录工具调用，正在核对授权或等待返回。'; icon = <ArrowUp size={16}/>;
  } else if (latestTool && stage === 'tool') {
    const state = toolResultState(latestTool);
    const sourceCount = reportedSourceCount(latestTool);
    title = `${toolResultLabels[state]} · ${toolLabel(String(latestTool.data.tool_name || '业务工具'))}`;
    detail = state === 'failed' ? (typeof latestTool?.data.error_message==='string' ? latestTool.data.error_message : '本次工具调用失败，正在整理已有信息。') : state === 'unknown' ? '未收到明确的调用结果，暂不报告业务结果。' : typeof sourceCount === 'number' && sourceCount > 0 ? `已登记 ${sourceCount} 个来源，内容仍需核对。` : '工具已返回，但未提供来源。';
    icon = state === 'failed' ? <XCircle size={16}/> : state === 'succeeded' ? <CheckCircle2 size={16}/> : <Circle size={16}/>;
  } else if (['scoping', 'routing', 'evidence'].includes(stage)) {
    const presentation = runStagePresentation(latestStage?.data || { stage });
    title = presentation.label; detail = presentation.detail;
    icon = presentation.confirmed ? <CheckCircle2 size={16}/> : <Circle size={16}/>;
  } else if (stage === 'initializing' || run.status === 'queued') {
    title = '正在准备执行环境'; detail = '确认助手配置、会话上下文和可用能力。';
  } else if (stage === 'runtime_ready') {
    title = '执行环境已就绪'; detail = '即将向模型提交当前任务。'; icon = <CheckCircle2 size={16}/>;
  } else if (stage === 'model_started') {
    title = '模型已开始响应'; detail = '正在整理回答，稍后将显示内容。';
  } else if (stage === 'answering' || events.some(event => event.type === 'message.delta')) {
    title = '正在生成回答'; detail = '回答将逐步显示，可在执行记录中查看工具与来源。';
  }
  const visible = [...events].filter(event => ['run.stage', 'tool.started', 'tool.completed', 'approval.required', 'approval.resolved', 'approval.model_decided', 'approval.dual_vote', 'memory.pending', 'memory.updated', 'memory.aborted', 'subagent.plan.created', 'subagent.started', 'subagent.progress', 'subagent.completed', 'subagent.failed', 'subagent.cancelled', 'subagent.plan.completed', 'subagent.approval.required', 'subagent.approval.denied', 'subagent.approval.resolved', 'context.compaction_started', 'context.compacted'].includes(event.type)).slice(-4);
  const eventLabel = (event: StreamEvent) => {
    if (event.type === 'run.stage') return runStagePresentation(event.data).label;
    if (event.type === 'memory.pending') return '记忆已暂存，等待任务成功后保存';
    if (event.type === 'memory.updated') return '记忆已正式保存';
    if (event.type === 'memory.aborted') return '记忆未保存，暂存已废弃';
    if (event.type === 'tool.started') return `调用 ${toolLabel(String(event.data.tool_name || '业务工具'))}`;
    if (event.type === 'tool.completed') return `${toolResultLabels[toolResultState(event)]} · ${toolLabel(String(event.data.tool_name || '业务工具'))}`;
    if (event.type === 'approval.required') return '等待操作确认';
    if (event.type === 'approval.resolved') return event.data.decision === 'allow' ? '已允许操作' : '未获授权';
    if (event.type === 'approval.model_decided') return event.data.decision === 'allow' ? '模型代审允许低风险操作' : '模型代审拒绝操作';
    if (event.type === 'approval.dual_vote') return `双人审批投票 ${String(event.data.votes || 0)}/${String(event.data.required || 2)}`;
    if (event.type === 'subagent.plan.created') return `已拆分 ${String(event.data.max_children || '若干')} 个子任务`;
    if (event.type === 'subagent.started') return '子任务开始执行';
    if (event.type === 'subagent.progress') return `子任务进度 ${String(event.data.progress || 0)}%`;
    if (event.type === 'subagent.completed') return '子任务已完成';
    if (event.type === 'subagent.failed') return '子任务未完成';
    if (event.type === 'subagent.cancelled') return '子任务已取消';
    if (event.type === 'subagent.plan.completed') return '子任务处理已结束';
    if (event.type === 'subagent.approval.required') return '子任务等待高风险审批';
    if (event.type === 'subagent.approval.denied') return '子任务审批被拒绝';
    if (event.type === 'subagent.approval.resolved') return event.data.decision === 'allow' ? '子任务审批已允许' : '子任务审批未获授权';
    if (event.type === 'context.compaction_started') return '开始整理上下文';
    return '上下文整理完成';
  };
  return <div className={`live-activity ${waiting ? 'waiting' : ''}`} aria-live="polite"><div className="live-activity-current"><span className="live-activity-icon">{icon}</span><div><strong>{title}</strong><p>{detail}</p></div></div>{visible.length > 0 && <div className="live-activity-trail" aria-label="当前执行脉络">{visible.map(event => <span key={event.id} className={event.type === 'tool.completed' && event.data.success === false ? 'failed' : ''}>{eventLabel(event)}</span>)}</div>}</div>;
}

function ApprovalCard({ approval, busy, onDecision }: { approval: Approval; busy: boolean; onDecision: (decision: 'allow' | 'deny') => void }) {
  const expired = new Date(approval.expires_at).getTime() < Date.now();
  const denyByDefault = approval.approval_policy === 'deny_by_default';
  const dual = approval.approval_mode === 'dual' || approval.strategy === 'dual';
  const votes = approval.approver_ids?.length || 0;
  const required = approval.required_approvals || 2;
  const riskLabel = approval.risk_level === 'low' ? '低风险' : approval.risk_level === 'medium' ? '中风险' : '高风险';
  return <section className="approval-card"><div className="approval-title"><ShieldCheck size={18}/><strong>{dual ? '高风险操作需要双人管理员审批' : '请确认这项操作'}</strong></div><h3>{toolLabel(approval.tool_name)}</h3><p className="approval-impact">{riskLabel} · {approval.impact || '执行前需要服务端重新校验授权'}</p>{dual && <p className="approval-impact">管理员投票进度：{votes}/{required}（必须是两名不同管理员）</p>}<details open><summary>操作参数（不可修改）<ChevronDown size={14}/></summary><pre>{JSON.stringify(approval.arguments, null, 2)}</pre></details><p>{denyByDefault ? '此工具未通过服务端授权，系统将拒绝执行。' : expired ? '此操作的确认请求已过期。' : `有效至 ${timeText(approval.expires_at)}`}</p><div className="approval-actions"><Button variant="secondary" disabled={busy || expired} onClick={() => onDecision('deny')}><X size={15}/>拒绝</Button><Button busy={busy} disabled={expired || denyByDefault} onClick={() => onDecision('allow')}><Check size={15}/>{dual ? '管理员投票允许' : '允许执行'}</Button></div></section>;
}

type ToolResultState = 'succeeded' | 'failed' | 'unknown';
const toolResultLabels: Record<ToolResultState, string> = { succeeded: '调用成功', failed: '调用失败', unknown: '结果未确认' };
const toolResultState = (event?: StreamEvent): ToolResultState => event?.data.success === true ? 'succeeded' : event?.data.success === false ? 'failed' : 'unknown';

export const runStagePresentation = (data: Record<string, unknown>): { label: string; detail: string; confirmed: boolean } => {
  const stage = String(data.stage || '');
  if (stage === 'scoping') return { label: '记录调用范围', detail: '调用前阶段记录；实际筛选范围以工具返回为准。', confirmed: false };
  if (stage === 'routing') return { label: '记录工具选择', detail: '调用前阶段记录；不代表已完成业务路由或知识检索。', confirmed: false };
  if (stage === 'model_retry') return { label: '模型连接中断，正在重试', detail: '已取得的业务数据会保留，你仍可以停止本次任务。', confirmed: false };
  if (stage === 'evidence') {
    const count = data.source_count;
    const reported = data.status === 'reported' && typeof count === 'number' && Number.isFinite(count) && count > 0;
    return reported
      ? { label: '来源已登记', detail: `已登记 ${count} 个来源，内容仍需核对。`, confirmed: true }
      : { label: '来源未提供', detail: '当前未收到明确的来源登记结果，不能据此确认业务结论。', confirmed: false };
  }
  const labels: Record<string, string> = { tool: '调用业务工具', initializing: '准备执行环境', runtime_ready: '执行环境就绪', model_started: '模型开始响应', answering: '开始生成回答' };
  return { label: labels[stage] || '执行阶段更新', detail: '', confirmed: false };
};

type ExecutionEvidence = {
  id: number | string;
  toolName: string;
  status: ToolResultState;
  domain: string;
  process: string;
  object: string;
  metric: string;
  timeRange: string;
  filters: string;
  returnedRows: string;
  originalRows: string;
  sourceCount: string;
  cache: string;
  error: string;
  modelInputBytes: string;
};

const unknownFact = '未提供';
const hasFactValue = (value: unknown): boolean => {
  if (value === undefined || value === null) return false;
  if (typeof value === 'string') return value.trim() !== '' && !['unknown', '未提供'].includes(value.trim().toLowerCase());
  if (Array.isArray(value)) return value.some(hasFactValue);
  if (typeof value === 'object') return Object.values(value).some(hasFactValue);
  return typeof value !== 'number' || Number.isFinite(value);
};
const factValue = (facts: Record<string, unknown>, keys: string[]): unknown => {
  for (const key of keys) {
    const value = facts[key];
    if (hasFactValue(value)) return value;
  }
  return undefined;
};
const factText = (facts: Record<string, unknown>, keys: string[]) => {
  const value = factValue(facts, keys);
  if (value === undefined) return unknownFact;
  if (Array.isArray(value)) return value.filter(hasFactValue).map(item => typeof item === 'string' ? item : JSON.stringify(item)).join('、');
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
};
const factNumber = (facts: Record<string, unknown>, keys: string[]) => {
  const value = factValue(facts, keys);
  return typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString('zh-CN') : unknownFact;
};

const reportedSourceCount = (event?: StreamEvent): number | undefined => {
  if (toolResultState(event) !== 'succeeded') return undefined;
  if (Array.isArray(event?.data.source_ids)) return new Set(event.data.source_ids.filter(id => typeof id === 'string' && id.trim())).size;
  const facts = event?.data.result_facts;
  const count = facts && typeof facts === 'object' ? (facts as Record<string, unknown>).source_count : undefined;
  return typeof count === 'number' && Number.isFinite(count) && count >= 0 ? count : undefined;
};

export const buildExecutionEvidence = (events: StreamEvent[], _run: Run): ExecutionEvidence => {
  const completed = [...events].reverse().find(event => event.type === 'tool.completed');
  return buildExecutionEvidenceForTool(completed);
};

const buildExecutionEvidenceForTool = (completed: StreamEvent | undefined): ExecutionEvidence => {
  const status = toolResultState(completed);
  const facts = status === 'succeeded' && completed?.data.result_facts && typeof completed.data.result_facts === 'object' ? completed.data.result_facts as Record<string, unknown> : {};
  const sourceCount = reportedSourceCount(completed);
  return {
    id: completed?.id ?? 'pending',
    toolName: toolLabel(typeof completed?.data.tool_name === 'string' && completed.data.tool_name.trim() ? completed.data.tool_name : '业务工具'),
    status,
    domain: factText(facts, ['domain', 'business_domain', 'businessDomain']),
    process: factText(facts, ['process', 'business_process', 'businessProcess']),
    object: factText(facts, ['object', 'business_object', 'businessObject']),
    metric: factText(facts, ['metric', 'metrics', 'indicator', 'indicators']),
    timeRange: factText(facts, ['time_range', 'timeRange', 'date_range', 'dateRange']),
    filters: factText(facts, ['filters', 'filter_summary', 'filterSummary']),
    returnedRows: factNumber(facts, ['returned_rows', 'returnedRows', 'row_count', 'rowCount']),
    originalRows: factNumber(facts, ['original_rows', 'originalRows', 'total_rows', 'totalRows']),
    sourceCount: typeof sourceCount === 'number' ? sourceCount.toLocaleString('zh-CN') : unknownFact,
    cache: typeof factValue(facts, ['cache_hit', 'cacheHit']) === 'boolean' ? (factValue(facts, ['cache_hit', 'cacheHit']) ? '命中' : '未命中') : unknownFact,
    error: typeof completed?.data.error_message === 'string' ? completed.data.error_message : '',
    modelInputBytes: factNumber(completed?.data || {}, ['model_input_bytes']),
  };
};

export const buildExecutionEvidences = (events: StreamEvent[], _run: Run): ExecutionEvidence[] =>
  events.filter(event => event.type === 'tool.completed').map(event => buildExecutionEvidenceForTool(event));

export function ExecutionEvidenceCard({ events, run }: { events: StreamEvent[]; run: Run }) {
  const evidences = buildExecutionEvidences(events, run);
  if (!evidences.length) return <section className="execution-evidence" aria-label="范围与证据状态"><div className="execution-evidence-heading"><div><span className="eyebrow">范围与证据</span><h3>等待工具返回后展示</h3></div><small>未返回的字段显示“未提供”</small></div><p className="section-help">当前还没有可核验的业务工具结果。</p></section>;
  return <section className="execution-evidence" aria-label="范围与证据状态"><div className="execution-evidence-heading"><div><span className="eyebrow">范围与证据</span><h3>工具返回的范围与来源</h3></div><small>未返回的字段显示“未提供” · {evidences.length} 次调用</small></div>{evidences.map(evidence => { const cells: Array<[string, string]> = [
    ['业务域', evidence.domain], ['流程', evidence.process], ['对象', evidence.object], ['指标', evidence.metric],
    ['时间范围', evidence.timeRange], ['筛选条件', evidence.filters], ['返回行数', evidence.returnedRows], ['原始行数', evidence.originalRows],
  ]; return <div className="execution-evidence-tool" key={evidence.id}><div className="execution-evidence-tool-title"><strong>{evidence.toolName}</strong><span className={`badge ${evidence.status === 'succeeded' ? 'green' : evidence.status === 'failed' ? 'amber' : ''}`}>{toolResultLabels[evidence.status]}</span></div>{evidence.status === 'succeeded' ? <><dl className="execution-evidence-grid">{cells.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl><div className="execution-evidence-meta"><span>来源 {evidence.sourceCount}</span><span>缓存 {evidence.cache}</span></div>{evidence.modelInputBytes!==unknownFact&&<p className="execution-evidence-note">提供给模型的数据 {evidence.modelInputBytes} 字节（UTF-8，非 Token）</p>}</> : <p className="execution-evidence-note">{evidence.status === 'failed' ? evidence.error || '本次调用失败，未展示业务结果。' : '调用结果尚未确认，业务范围与来源保持未提供。'}</p>}</div>; })}</section>;
}

export function EventRow({ event }: { event: StreamEvent }) {
  let label = ''; let detail = ''; let icon = <Circle size={14}/>;
  if (event.type === 'run.started') { label = '开始执行'; icon = <Radio size={14}/>; }
  if (event.type === 'run.stage') { const presentation = runStagePresentation(event.data); label = presentation.label; detail = presentation.detail; icon = presentation.confirmed ? <CheckCircle2 size={14}/> : <Clock3 size={14}/>; }
  if (event.type === 'tool.started') { label = toolLabel(String(event.data.tool_name || '调用工具')); icon = <ArrowUpRightIcon/>; }
  if (event.type === 'tool.completed') { const state = toolResultState(event); const sourceCount = reportedSourceCount(event); label = `${toolResultLabels[state]} · ${toolLabel(String(event.data.tool_name || '工具调用'))}`; detail = state === 'failed' ? (typeof event.data.error_message==='string' ? event.data.error_message : '未取得成功的工具结果。') : state === 'unknown' ? '未收到明确的调用结果，暂不报告业务结果。' : typeof sourceCount === 'number' && sourceCount > 0 ? `已登记 ${sourceCount} 个来源，内容仍需核对。` : '未提供来源。'; icon = state === 'succeeded' ? <CheckCircle2 size={14}/> : state === 'failed' ? <XCircle size={14}/> : <Circle size={14}/>; }
  if (event.type === 'approval.resolved') { label = event.data.decision === 'allow' ? '已允许操作' : '已拒绝操作'; const source = String(event.data.decision_source || ''); detail = source && source !== 'user' ? `决策来源：${source === 'timeout' ? '超时' : source === 'cancelled' ? '任务取消' : source === 'restart' ? '服务重启' : source === 'interrupted' ? '执行中断' : '运行结束'}` : ''; icon = <ShieldCheck size={14}/>; }
  if (event.type === 'memory.pending') { label = '记忆已暂存，尚未保存'; icon = <Clock3 size={14}/>; }
  if (event.type === 'memory.updated') { label = '记忆已正式保存'; detail = event.data.context_reset===true?'下一轮将使用新的上下文，不再自动带入旧消息、旧记忆和旧附件。':'记忆内容已确认。'; icon = <CheckCircle2 size={14}/>; }
  if (event.type === 'memory.aborted') { label = '记忆未保存，暂存已废弃'; icon = <XCircle size={14}/>; }
  if (event.type === 'artifact.created') { label = `已生成 ${String(event.data.name || '文件')}`; icon = <FileText size={14}/>; }
  if (event.type === 'context.compaction_started') { label = '开始原生压缩'; icon = <Minimize2 size={14}/>; }
  if (event.type === 'context.compacted') { label = '已发生原生上下文压缩'; icon = <CheckCircle2 size={14}/>; }
  if (event.type === 'context.compaction_result') { label = compactionStatusLabel(event.data.status, event.data.followup_status); icon = <Minimize2 size={14}/>; }
  if (event.type === 'run.completed') { label = statusLabels[event.data.status as RunStatus] || '执行结束'; icon = event.data.status === 'succeeded' ? <CheckCircle2 size={14}/> : <Square size={12}/>; }
  return <div className={`event-row ${event.type === 'tool.completed' && event.data.success === false ? 'failed' : ''}`}><span className="event-icon">{icon}</span><div><span>{label}</span><small>{timeText(event.created_at)}</small>{detail && <details><summary>查看结果摘要<ChevronDown size={12}/></summary><pre>{detail}</pre></details>}{event.type === 'tool.started' && event.data.arguments != null && <details><summary>查看调用参数<ChevronDown size={12}/></summary><pre>{JSON.stringify(event.data.arguments, null, 2)}</pre></details>}{event.type === 'artifact.created' && typeof event.data.id === 'string' && <a className="inline-download" href={fileURL(event.data.id)} download><Download size={13}/>下载</a>}</div></div>;
}
function ArrowUpRightIcon() { return <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M7 17 17 7M7 7h10v10"/></svg>; }

export function RunMetrics({ run }: { run: Run }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => { if (!isActive(run.status)) return; const timer = window.setInterval(() => setNow(Date.now()), 1000); return () => window.clearInterval(timer); }, [run.status]);
  const elapsedMs = (run.completed_at ? new Date(run.completed_at).getTime() : now) - new Date(run.created_at).getTime();
  const seconds = Math.max(0, Math.floor(elapsedMs / 1000));
  const measuredTotal = !isActive(run.status) && typeof run.timings?.total_ms === 'number' && Number.isFinite(run.timings.total_ms) && run.timings.total_ms >= 0 ? run.timings.total_ms : null;
  const duration = measuredTotal !== null ? `${(measuredTotal / 1000).toFixed(2)} 秒` : !Number.isFinite(elapsedMs) ? '未提供' : seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
  const metadata=run.execution_kind==='product_metadata';
  const usage = run.usage;
  const tokenCount = (value: unknown): number | null => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : null;
  const record = (value: unknown): Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
  const accounting = usage?.token_accounting !== null && typeof usage?.token_accounting === 'object' ? record(usage.token_accounting) : null;
  const stages = accounting && Array.isArray(accounting.stages) ? accounting.stages.filter(item => item && typeof item === 'object') as Array<Record<string, unknown>> : [];
  const tokens = (key: string): string => {
    if (metadata) return '0';
    const subtotalLabel = (value: number) => `已知小计 ${value.toLocaleString('zh-CN')}，完整用量未提供`;
    if (!accounting) {
      const known = tokenCount(usage?.[key]);
      return known === null ? '未提供' : subtotalLabel(known);
    }
    const contributions = stages.map(stage => tokenCount(record(stage.provider_usage)[key])).filter((value): value is number => value !== null);
    const reportedTotal = tokenCount(record(accounting.provider_totals)[key]);
    // An empty ledger initializes totals to zero; that is not a provider receipt.
    const total = reportedTotal !== null && contributions.length > 0 ? reportedTotal : null;
    if (run.status === 'succeeded' && total !== null) return total.toLocaleString('zh-CN');
    // A stage-level complete flag says nothing about an interrupted call after it.
    // Only a succeeded run can present these recorded totals as complete run usage.
    const known = contributions.length > 0 ? tokenCount(record(accounting.known_subtotals)[key]) ?? contributions.reduce((sum, value) => sum + value, 0) : total;
    return known === null ? '未提供' : subtotalLabel(known);
  };
  const cost = usage?.estimated_cost_usd;
  const costText = metadata ? '$0.0000' : typeof cost !== 'number' || !Number.isFinite(cost) || cost < 0 ? '未提供' : run.status === 'succeeded' && (!accounting || accounting.provider_usage_complete === true) ? `$${cost.toFixed(4)}` : `已知估算小计 $${cost.toFixed(4)}，完整费用未提供`;
  const stageTokens = (stage: Record<string, unknown>) => { const provider = record(stage.provider_usage); const value = (key: string) => { const count = tokenCount(provider[key]); return count === null ? '未提供' : count.toLocaleString('zh-CN'); }; return `入 ${value('input_tokens')} · 出 ${value('output_tokens')} · 缓 ${value('cache_read_input_tokens')}`; };
  return <section className="run-metrics" aria-label="本次运行用量"><dl>{metadata&&<><dt>执行方式</dt><dd>模型信息查询</dd><dt>模型调用</dt><dd>未调用模型</dd></>}<dt>{measuredTotal !== null ? '总耗时（含等待）' : '已用时间（按时间差）'}</dt><dd>{duration}</dd>{run.timings&&Object.entries({first_server_progress_ms:'服务端首个进度',first_tool_attempt_ms:'首个工具调用尝试',first_tool_success_ms:'首个工具成功返回',runtime_ready_ms:'运行环境就绪',first_model_event_ms:'首个模型事件',first_text_ms:'首段正文'}).map(([key,label])=>{const value=run.timings?.[key as keyof typeof run.timings];return typeof value==='number'?<div className="timing-row" key={key}><dt>{label}</dt><dd>{(value/1000).toFixed(2)} 秒</dd></div>:null;})}<dt>输入 Token</dt><dd>{tokens('input_tokens')}</dd><dt>输出 Token</dt><dd>{tokens('output_tokens')}</dd><dt>缓存读取 Token</dt><dd>{tokens('cache_read_input_tokens')}</dd><dt>费用估算</dt><dd>{costText}</dd>{stages.length>0&&<><dt>Token 阶段</dt><dd><div className="token-stage-list">{stages.map((stage,index)=><span key={`${String(stage.stage||'stage')}-${index}`}><strong>{String(stage.stage||'未知')}</strong> · {String(stage.source||'未知')}<small>{stageTokens(stage)}</small></span>)}</div></dd></>}</dl><a className="trace-download" href={`${API_BASE}/runs/${encodeURIComponent(run.id)}/trace`} download={`run-${run.id}.json`}><Download size={14}/>下载执行记录 JSON</a></section>;
}
