import { Fragment, useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { BookOpen, Archive, ArrowDown, ArrowUp, Check, CheckCircle2, ChevronDown, Circle, Clock3, Download, FileText, Gauge, ListTree, LoaderCircle, Minimize2, Paperclip, Pencil, Radio, ShieldCheck, Sparkles, Square, X, XCircle } from 'lucide-react';
import { api, API_BASE, ApiError, chatMessagePayload, clientRequestId, errorText, fileURL, patch, post, remove, sendChatMessage, timeText } from './api';
import { configAllows, type AppConfig, type Approval, type Assistant, type Conversation, type Message, type ModelOption, type ModelRecommendation, type ModelSelection, type Run, type RunStatus, type Skill, type StoredFile, type StreamEvent } from './types';
import { isActive, statusLabels } from './types';
import { appendEvent, eventTypes, pendingApprovalIds, streamText } from './stream';
import { Attachment, Button, ErrorNotice, Field, FileChip, FilePreview, Loader, Modal } from './components';
import { compactionStatus, compactionStatusLabel, toolLabel } from './display';
import ContextDialog, { compactionLabel } from './ContextDialog';
import SessionTrajectory from './SessionTrajectory';
import ConversationExport from './ConversationExport';
import ModelSelector from './ModelSelector';
import { ModelRoutingControl, ModelSettingsDisclosure, RunModelIdentity } from './ModelRouting';
import SessionPermissionMode, { MCPAvailabilityNotice } from './SessionPermissionMode';
import { conversationHasModelHistory, modelSwitchRequiresNewConversation, saveModelRouting, saveModelSelection } from './models';
import ModelRecovery from './ModelRecovery';
import AiIdentity from './AiIdentity';
import MemoryApprovalCard, { MEMORY_WRITE_TOOL } from './MemoryApprovalCard';
import { AnswerMarkdown } from './Sources';
import BusinessResults, { questionForRun } from './BusinessResult';
import HistoricalBusinessResults, { historicalResultMessages } from './HistoricalBusinessResults';
import SkillPicker, { mentionQuery, selectedSkillIds, SkillChips } from './SkillPicker';
import { ComposerCompletion, ComposerFollowups, composerInputMaxHeight, composerShortcut, useComposerAssistance } from './ComposerAssistance';
import MessageQueue, { useMessageQueue } from './MessageQueue';
import { CHAT_ATTACHMENT_ACCEPT, chatAttachmentProblem } from './attachmentPolicy';
import { clipboardFiles } from './clipboardAttachments';
import { useComposerDraft } from './composerDraft';
import { useComposerUploads } from './composerUploads';
import { MemoryWorkspace } from './MemoryPage';
import { pendingRun, retryRead, transientReadError } from './runReadback';
import RunOutcome from './RunOutcome';
import RunInteractions from './RunInteractions';
import ConversationTasks from './ConversationTasks';
import InlineSubagentRun from './InlineSubagentRun';
import ComposerReferences from './ComposerReferences';
import { readChatHistory, useChatHistory } from './chatHistory';
import './chat-history.css';
import './composer.css';
import './assistant-working.css';

type SteeringReceipt = { id: string; text: string; status: 'pending' | 'queued' | 'applied' | 'rejected' | 'uncertain' };

// Keep routing input bounded while deriving it from the current task context.
export const fitnessTaskSummary=(input:string,messages:Message[],assistantDescription='')=>{
  const direct=input.trim();
  const recent=messages.filter(message=>message.role==='user'&&message.text.trim()).slice(-4).map(message=>message.text.trim());
  const context=[...recent, ...(direct?[direct]:[])].join('\n').slice(-2000);
  return context || assistantDescription.trim().slice(0,1900) || '根据当前会话上下文选择最合适的模型。';
};
export const loadChatBootstrap=async(conversationId:string,signal?:AbortSignal,targetMessageId?:string)=>{
  let missingTarget = false;
  const [history,detail]=await Promise.all([
    readChatHistory(conversationId, targetMessageId ? {around:targetMessageId} : undefined, signal).catch(error => {
      if (targetMessageId && error instanceof ApiError && error.code === 'MESSAGE_NOT_FOUND') {
        missingTarget = true;
        return readChatHistory(conversationId, undefined, signal);
      }
      throw error;
    }),
    api<Conversation>(`/conversations/${conversationId}`, { signal }),
  ]);
  const runId=detail.active_run_id||history.latest_run_id;
  let run:Run|null=null;let runError:unknown=null;
  if(runId){
    try{run=await api<Run>(`/runs/${runId}`, { signal });}
    catch(error){runError=error;}
  }
  return {history,detail,run,runError,runId,missingTarget};
};
export const loadChatSkills=async(enabled:boolean,assistantId:string,signal?:AbortSignal):Promise<Skill[]>=>enabled?api<Skill[]>(`/assistants/${assistantId}/skills`,{signal}):[];
export { chatMessagePayload, sendChatMessage } from './api';
export const recoveryMessagePayload=(runId:string,requestId:string)=>({client_request_id:requestId,recovery_run_id:runId});
export function subagentRunForQuestion(message: Message, liveQuestionId?: string, liveRunId?: string): string | null {
  if (message.role !== 'user' || (!message.has_subagents && message.id !== liveQuestionId)) return null;
  return message.subagent_run_id || message.run_id || (message.id === liveQuestionId ? liveRunId ?? null : null);
}
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

export default function ChatPage({ conversation, assistant, config, models, modelsLoading, initialDraft, initialRequestId, draftOwnerId, targetMessageId, onOpenConversation, onChanged, onArchived, onNewConversation, onMemoryDirtyChange, onMemorySavingChange }: { conversation: Conversation; assistant?: Assistant; config: AppConfig; models: ModelOption[]; modelsLoading: boolean; initialDraft?: string; initialRequestId?: string; draftOwnerId?: string; targetMessageId?: string; onOpenConversation?: (conversation: Conversation) => void; onChanged: () => void; onArchived: () => void; onNewConversation?: (selection:ModelSelection,draft:string)=>void; onMemoryDirtyChange?: (dirty:boolean)=>void; onMemorySavingChange?: (saving:boolean)=>void }) {
  const trial = config.deployment_mode === 'trial'; const canSend = configAllows(config, 'messages.send'); const {canUseAttachments,canUseSkills,canUseCompletion,canUseContext,canRecommend} = chatTrialFeatures(trial,config);
  const history = useChatHistory(conversation.id);
  const { messages } = history.page;
  const [run, setRun] = useState<Run | null>(null); const [events, setEvents] = useState<StreamEvent[]>([]);
  const [archived,setArchived]=useState(conversation.archived);const [restoring,setRestoring]=useState(false);
  const [showContext, setShowContext] = useState(false);
  const [showTrajectory,setShowTrajectory]=useState(false);
  const [showTasks,setShowTasks]=useState(false);
  const [forkBoundary, setForkBoundary] = useState<string>();
  const [showReferences,setShowReferences]=useState(false);
  const [skills, setSkills] = useState<Skill[]>([]); const [skillQuery, setSkillQuery] = useState<string|null>(null);const [skillHighlight,setSkillHighlight]=useState(0); const mention = useRef<ReturnType<typeof mentionQuery>>(null); const [runStage,setRunStage]=useState('');
  useEffect(()=>{let current=true;loadChatSkills(canUseSkills,conversation.assistant_id).then(result=>{if(current)setSkills(result);}).catch(()=>{});return()=>{current=false;};},[conversation.assistant_id,canUseSkills]);
  const [selection, setSelection] = useState<ModelSelection>({ model_id: conversation.model_id, reasoning_level: conversation.reasoning_level });
  const [autoModelRouting, setAutoModelRouting] = useState(!trial && Boolean(conversation.auto_model_routing));
  const [recommendOpen, setRecommendOpen] = useState(false); const [recommendSummary, setRecommendSummary] = useState(''); const [recommendation, setRecommendation] = useState<ModelRecommendation | null>(null); const [recommendBusy, setRecommendBusy] = useState(false);
  const [savingModel, setSavingModel] = useState(false);
  const [routingSaved, setRoutingSaved] = useState(false);
  const selectionVersion = useRef(0);
  const [hasNativeSession, setHasNativeSession] = useState(conversation.has_native_session);
  const [modelReconnectRequired, setModelReconnectRequired] = useState(false);
  const [refreshingRun, setRefreshingRun] = useState(false);
  const [readbackIssue, setReadbackIssue] = useState('');
  const retryReadback = useRef<(() => void) | null>(null);
  const [loading, setLoading] = useState(true); const [error, setError] = useState(''); const [sending, setSending] = useState(false); const [retrying, setRetrying] = useState(false);
  const steeringRequest = useRef<{ runId: string; text: string; id: string } | null>(null);
  const [steeringReceipt, setSteeringReceipt] = useState<{ runId: string; value: SteeringReceipt } | null>(null);
  const { draft: { text: input, files, skills: selectedSkills, preview }, readDraft, setText: setInput, setFiles,
    setSkills: setSelectedSkills, setPreview, setSubmission, finishSubmission } = useComposerDraft(draftOwnerId, conversation.id, initialDraft || '', initialRequestId && initialDraft
      ? { id: initialRequestId, signature: JSON.stringify([initialDraft.trim(), [], []]), queued: false } : undefined);
  const saveAttachment = useCallback((saved: StoredFile, selectionOrder: number) => setFiles(previous => {
    if (previous.some(file => file.id === saved.id)) return previous;
    const position = previous.findIndex(file => file.selectionOrder !== undefined && file.selectionOrder > selectionOrder);
    const next = previous.slice();
    next.splice(position === -1 ? next.length : position, 0, { ...saved, selectionOrder });
    return next;
  }), [setFiles]);
  const { queue: attachmentQueue, jobs: attachmentJobs, uploading, restoring: restoringUploads, restorationError } = useComposerUploads(canUseAttachments ? draftOwnerId : undefined, conversation.id, saveAttachment);
  const failedAttachments = attachmentJobs.some(job => job.status === 'failed');
  const [title, setTitle] = useState(conversation.title); const [titleDraft, setTitleDraft] = useState(conversation.title); const titleVersion = useRef(0); const [rename, setRename] = useState(false); const [archive, setArchive] = useState(false); const [savingTitle, setSavingTitle] = useState(false); const [connectionState, setConnectionState] = useState<'connected' | 'reconnecting'>('connected'); const [stopping, setStopping] = useState(false); const [decisionBusy, setDecisionBusy] = useState('');
  const composerForm = useRef<HTMLFormElement>(null); const composing = useRef(false); const inputVersion = useRef(0);
  const recall = useRef(-1);
  const submissionPending = useRef(false);
  const uploadPending = useRef(false);
  uploadPending.current = uploading;
  const retryRequest = useRef<{ signature: string; id: string } | null>(null);
  const scroll = useRef<HTMLDivElement>(null); const shouldFollow = useRef(true); const [showScroll, setShowScroll] = useState(false); const fileInput = useRef<HTMLInputElement>(null); const textInput = useRef<HTMLTextAreaElement>(null); const mounted = useRef(true); const onChangedRef = useRef(onChanged); onChangedRef.current = onChanged;
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const applyRunReadback = useCallback((next: Run) => {
    if (!mounted.current) return;
    // HTTP snapshots can arrive after a terminal SSE event or a newer task.
    // A read refreshes the selected run; it cannot select an old run or revive it.
    setRun(current => !current || current.id !== next.id || (!isActive(current.status) && isActive(next.status)) ? current : next);
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    let current = true;
    const requestedTitleVersion = titleVersion.current;
    const requestedSelectionVersion = selectionVersion.current;
    const load = async () => {
      try {
        const {history:startingPage,detail,run:loadedRun,runError,runId,missingTarget} = await retryRead(() => loadChatBootstrap(conversation.id, controller.signal, targetMessageId), controller.signal);
        if (!current) return;
        shouldFollow.current = !targetMessageId || missingTarget;
        history.setPage(startingPage);
        if (missingTarget) setError('该消息不可用，已为你打开最新消息。');
        setForkBoundary(detail.fork_inherited_through_id);
        setHasNativeSession(detail.has_native_session);setArchived(detail.archived);
        // A rename can finish before this initial GET. Its older snapshot must not undo it.
        if (titleVersion.current === requestedTitleVersion) setTitle(detail.title);
        if (selectionVersion.current === requestedSelectionVersion) { setSelection({ model_id: detail.model_id, reasoning_level: detail.reasoning_level }); setAutoModelRouting(!trial && Boolean(detail.auto_model_routing)); }
        if (loadedRun) setRun(loadedRun);
        else if (runId && transientReadError(runError)) setRun(pendingRun(runId, conversation.id));
        else if (runError) setError(errorText(runError));
      } catch (e) { if (current) setError(errorText(e)); }
      finally { if (current) setLoading(false); }
    };
    void load();
    return () => { current = false; controller.abort(); };
  }, [conversation.id, targetMessageId]);
  useEffect(() => {
    if (!run?.id) return;
    const runId = run.id;
    const runTitleVersion = titleVersion.current, runSelectionVersion = selectionVersion.current;
    const controller = new AbortController(), { signal } = controller;
    const seen = new Set<number>();
    let current = true, finalizing = false, terminal = false, statusPending = false, sawTerminalEvent = false, after = 0;
    let source: EventSource | undefined, timer: number | undefined;
    setEvents([]); setConnectionState('connected'); setReadbackIssue('');
    const readRun = () => api<Run>(`/runs/${runId}`, { signal });
    const finalize = async () => {
      if (finalizing || !current) return;
      terminal = true; finalizing = true;
      source?.close(); window.clearTimeout(timer); window.clearInterval(watchStatus);
      setRefreshingRun(true); setReadbackIssue('');
      try {
        const [nextRun, nextMessages, detail] = await retryRead(async () => {
          const result = await Promise.all([readRun(), readChatHistory(conversation.id, undefined, signal), api<Conversation>(`/conversations/${conversation.id}`, { signal })]);
          if (isActive(result[0].status)) throw new ApiError('任务结果尚未就绪。', 503, 'RUN_READBACK_PENDING');
          return result;
        }, signal);
        if (!current) return;
        applyRunReadback(nextRun); history.reconcileLatest(nextMessages);
        if (titleVersion.current === runTitleVersion) setTitle(detail.title);
        setHasNativeSession(detail.has_native_session);
        if (selectionVersion.current === runSelectionVersion) {
          setSelection({ model_id: detail.model_id, reasoning_level: detail.reasoning_level });
          setAutoModelRouting(!trial && Boolean(detail.auto_model_routing));
        }
        setConnectionState('connected'); onChangedRef.current();
        if (!sawTerminalEvent && configAllows(config, 'runs.read')) {
          // If HTTP polling observed completion before EventSource, replace
          // its missing final tool/artifact events from the durable trace.
          void api<{events: StreamEvent[]}>(`/runs/${runId}/trace`, { signal }).then(trace => {
            if (!current || !Array.isArray(trace.events)) return;
            setEvents(previous => trace.events.reduce(appendEvent, previous));
          }).catch(() => {});
        }
      } catch (e) {
        if (current) { setConnectionState('reconnecting'); setReadbackIssue(`暂未读取到完整任务结果。${errorText(e)}`); }
      } finally {
        finalizing = false;
        if (current) setRefreshingRun(false);
      }
    };
    const readStatus = async () => {
      if (!current || terminal || statusPending) return;
      statusPending = true;
      try {
        const snapshot = await retryRead(readRun, signal);
        if (!current || terminal) return;
        applyRunReadback(snapshot); setReadbackIssue('');
        if (!isActive(snapshot.status)) await finalize();
      } catch (e) {
        if (current && !terminal) setReadbackIssue(`暂未读取到任务状态。${errorText(e)}`);
      } finally { statusPending = false; }
    };
    const watchStatus = window.setInterval(() => {
      if (document.visibilityState === 'visible') void readStatus();
    }, 8000);
    const receive = (raw: Event) => {
      if (!current) return;
      let event: StreamEvent;
      try { event = JSON.parse((raw as MessageEvent).data) as StreamEvent; } catch { setError('收到无法识别的执行事件，请刷新会话重新读取。'); return; }
      if (event.run_id !== runId || seen.has(event.id)) return; seen.add(event.id); after = Math.max(after, event.id);
      setEvents(prev => appendEvent(prev, event));
      if (event.type === 'steer.applied') history.addAccepted(event.data as unknown as Message, false);
      if (event.type === 'timing.updated') setRun(prev => prev ? {...prev,timings:{...prev.timings,...event.data}} : prev);
      if (event.type === 'run.stage') setRunStage(String(event.data.stage||''));
      if (event.type === 'usage.updated') setRun(prev => prev ? { ...prev, usage: event.data } : prev);
      if (event.type === 'run.started') {setRunStage('initializing');}
      if (event.type === 'run.started') setRun(prev => prev?.status === 'queued' ? { ...prev, status: 'running' } : prev);
      if (event.type === 'approval.required') { setRun(prev => prev && isActive(prev.status) && prev.status !== 'cancelling' ? { ...prev, status: 'awaiting_approval' } : prev); }
      if (event.type === 'approval.resolved') setRun(prev => prev && isActive(prev.status) && prev.status !== 'cancelling' ? { ...prev, status: 'running', approvals: prev.approvals.filter(a => a.id !== event.data.approval_id) } : prev);
      if (['context.compaction_started', 'context.compacted', 'context.compaction_result'].includes(event.type)) setRun(prev => prev ? { ...prev, compaction: { trigger: null, pre_tokens: null, before: null, after: null, ...prev.compaction, status: event.type === 'context.compaction_started' ? 'started' : event.type === 'context.compacted' ? 'completed' : compactionStatus(event.data.status), error: typeof event.data.error === 'string' ? event.data.error : null, followup_status: typeof event.data.followup_status === 'string' ? event.data.followup_status : prev.compaction?.followup_status } } : prev);
      if (event.type === 'run.completed') { sawTerminalEvent = true; setRun(prev => prev ? { ...prev, status: event.data.status as RunStatus, error: typeof event.data.error === 'string' ? event.data.error : null, error_code: typeof event.data.error_code === 'string' ? event.data.error_code : null } : prev); void finalize(); }
    };
    const openEvents = () => {
      if (!current || terminal) return;
      const connection = new EventSource(`${API_BASE}/runs/${encodeURIComponent(runId)}/events?after=${after}`, { withCredentials: true });
      source = connection;
      eventTypes.forEach(type => connection.addEventListener(type, receive));
      connection.onopen = () => { if (current && !terminal) { setConnectionState('connected'); void readStatus(); } };
      connection.onerror = () => {
        if (!current || terminal) return;
        setConnectionState('reconnecting'); void readStatus();
        // Browsers retry transient disconnects. A terminal HTTP error closes
        // EventSource, so reopen it from the last processed event ourselves.
        if (connection.readyState === 2) {
          window.clearTimeout(timer);
          timer = window.setTimeout(openEvents, 1500);
        }
      };
    };
    retryReadback.current = () => { if (terminal) void finalize(); else void readStatus(); };
    openEvents();
    // Restore approval and cancel controls even if the initial status GET or
    // the stream's own handshake fails before reporting the first event.
    if (isActive(run.status)) void readStatus();
    return () => { current = false; controller.abort(); window.clearTimeout(timer); window.clearInterval(watchStatus); source?.close(); retryReadback.current = null; };
  // Only a different run starts a different event subscription.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run?.id, conversation.id, applyRunReadback]);
  const historyResultIds = configAllows(config, 'runs.read') ? historicalResultMessages(messages, conversation.id, run?.id) : new Set<string>();
  const liveText = streamText(events); const active = !!run && isActive(run.status); const pendingIds = pendingApprovalIds(events);
  const liveSubagentQuestion = !trial && run && events.some(event => event.run_id === run.id && event.type === 'subagent.plan.created')
    ? run.message_id || messages.find(message => message.role === 'user' && message.run_id === run.id)?.id
    : undefined;
  const currentSteeringEvent = [...events].reverse().find(event => event.type === 'steer.updated' && event.run_id === run?.id);
  const localSteering = steeringReceipt?.runId === run?.id ? steeringReceipt?.value : null;
  const remoteSteering = currentSteeringEvent?.data as SteeringReceipt | undefined;
  const steering = localSteering && localSteering.id !== remoteSteering?.id ? localSteering : remoteSteering || localSteering;
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
  const showThinking = (!active && sending) || (connectionState === 'connected' && !showStream && (run?.status === 'queued' || run?.status === 'running'));
  const artifacts = events.filter(e => e.type === 'artifact.created').map(e => e.data as unknown as StoredFile);
  const locatedTarget = useRef<string | null>(null);
  useLayoutEffect(() => {
    const viewport = scroll.current;
    if (!viewport || loading) return;
    if (!targetMessageId) locatedTarget.current = null;
    const target = viewport.querySelector<HTMLElement>('[data-search-target]');
    if (target && locatedTarget.current !== targetMessageId) {
      locatedTarget.current = targetMessageId || null;
      target.scrollIntoView({ block: 'center', behavior: 'instant' });
      shouldFollow.current = false;
      return;
    }
    const anchor = history.keepScroll.current;
    if (anchor) {
      history.keepScroll.current = null;
      const row = Array.from(viewport.querySelectorAll<HTMLElement>('[data-message-id]')).find(item => item.dataset.messageId === anchor.id);
      if (row) viewport.scrollTo({ top: viewport.scrollTop + row.getBoundingClientRect().top - anchor.top, behavior: 'instant' });
      return;
    }
    if (shouldFollow.current && !history.page.after) viewport.scrollTo({ top: viewport.scrollHeight, behavior: 'instant' });
  }, [messages, liveText, loading, events.length, targetMessageId, history.page.after, history.keepScroll]);
  const readHistory = (direction: 'older' | 'newer') => void history.read(direction, () => {
    const viewport = scroll.current;
    if (!viewport) return;
    const viewportTop = viewport.getBoundingClientRect().top;
    const row = Array.from(viewport.querySelectorAll<HTMLElement>('[data-message-id]')).find(item => item.getBoundingClientRect().bottom > viewportTop);
    shouldFollow.current = false;
    if (row?.dataset.messageId) history.keepScroll.current = { id: row.dataset.messageId, top: row.getBoundingClientRect().top };
  });
  const returnToLatest = () => {
    if (history.page.after) { void history.read('latest', () => { shouldFollow.current = true; setShowScroll(false); }); return; }
    if (scroll.current) scroll.current.scrollTo({ top: scroll.current.scrollHeight, behavior: 'smooth' });
    shouldFollow.current = true;
  };
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
    settled: !loading && !refreshingRun && !sending, run, messages, assistantName: assistant?.name, onDraft: fillDraft });
  const changeDraft = (value: string, caret: number) => {
    recall.current = -1;
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
  const openReferences = () => { closeModelSettings(); setSkillQuery(null); setShowReferences(true); };
  const insertReference = (value: string) => {
    const match = mention.current;
    const caret = textInput.current?.selectionStart ?? input.length;
    const start = match?.start ?? caret;
    const end = match?.end ?? textInput.current?.selectionEnd ?? caret;
    fillDraft(input.slice(0, start) + value + input.slice(end));
    setShowReferences(false);
    requestAnimationFrame(() => { textInput.current?.focus(); textInput.current?.setSelectionRange(start + value.length, start + value.length); });
  };
  const referenceFile = (file: StoredFile) => {
    const runtimeType = conversation.runtime_type || 'claude';
    const maximum = runtimeType === 'harness' ? config.chat_attachment_upload_max_bytes || config.upload_max_bytes : config.upload_max_bytes;
    const inlineLegacy = runtimeType === 'claude' && models.find(model => model.id === selection.model_id)?.capabilities?.includes('multimodal');
    const next = files.some(item => item.id === file.id) ? files : [...files, file];
    const problem = chatAttachmentProblem([...next, ...attachmentQueue.read().map(job => job.file)], runtimeType, maximum, config.chat_attachment_limits,
      inlineLegacy ? config.legacy_chat_image_max_bytes : undefined);
    if (problem) { setError(problem); setShowReferences(false); return; }
    setFiles(next);
    insertReference(`「${file.name}」 `);
  };
  const commands = [
    ...(canUseContext ? [{ name: '/上下文', alias: '/context', open: () => setShowContext(true) }] : []),
    { name: '/引用', alias: '/reference', open: openReferences },
    { name: '/计划任务', alias: '/schedule', open: () => setShowTasks(true) },
  ];
  const slash = !trial && /^\/[^\s/]*$/.test(input) && !sending && !archived
    ? commands.filter(command => command.name.startsWith(input) || command.alias.startsWith(input.toLowerCase())) : [];
  const runCommand = (open: () => void) => { fillDraft(''); open(); };
  const recallDraft = (direction: 'ArrowUp' | 'ArrowDown') => {
    if (trial || archived || loading || sending || files.length || selectedSkills.length || (recall.current < 0 && (input || direction === 'ArrowDown'))) return false;
    const previous = [...new Set(messages.slice().reverse().filter(message => message.role === 'user' && message.text.trim()).map(message => message.text))];
    if (!previous.length) return false;
    recall.current = Math.max(-1, Math.min(previous.length - 1, recall.current + (direction === 'ArrowUp' ? 1 : -1)));
    assistance.invalidate();
    fillDraft(previous[recall.current] || '');
    return true;
  };
  const [forking, setForking] = useState<string | null>(null);
  const forkRequest = useRef<{ messageId: string; requestId: string } | null>(null);
  const forkMessage = async (message: Message) => {
    if (forking || active || !onOpenConversation) return;
    if (forkRequest.current?.messageId !== message.id) forkRequest.current = { messageId: message.id, requestId: clientRequestId() };
    setForking(message.id); setError('');
    try {
      const child = await post<Conversation>(`/conversations/${conversation.id}/fork`, {
        message_id: message.id, client_request_id: forkRequest.current.requestId,
      });
      if (mounted.current) { onOpenConversation(child); onChanged(); }
    } catch (cause) {
      if (mounted.current) setError(errorText(cause));
    } finally {
      if (mounted.current) setForking(null);
    }
  };
  const canSteer = !trial && conversation.runtime_type === 'harness' && run?.status === 'running'
    && (!run.kind || run.kind === 'chat') && !archived && !loading && !showContext && canSend;
  const steer = async () => {
    const text = input.trim();
    if (!run || !canSteer || submissionPending.current || !text || text.length > 4000 || files.length || selectedSkills.length || uploading) return;
    const runId = run.id;
    if (steeringRequest.current?.runId !== runId || steeringRequest.current.text !== text) {
      steeringRequest.current = { runId, text, id: clientRequestId() };
    }
    const version = inputVersion.current;
    submissionPending.current = true;
    setSending(true);
    setError('');
    try {
      const value = await post<SteeringReceipt>(`/runs/${runId}/steering`, { text, client_request_id: steeringRequest.current.id });
      if (!mounted.current) return;
      setSteeringReceipt({ runId, value });
      if (value.status === 'queued' || value.status === 'applied') {
        if (inputVersion.current === version) fillDraft('');
        steeringRequest.current = null;
      }
    } catch (failure) {
      if (mounted.current) setError(errorText(failure));
    } finally {
      submissionPending.current = false;
      if (mounted.current) setSending(false);
    }
  };
  const send = async () => {
    if (failedAttachments) { setError('有附件未上传成功，请重试或移除后再发送。'); return; }
    if (!canSend || (!input.trim() && files.length === 0 && selectedSkills.length===0) || archived || sending || submissionPending.current || uploadPending.current || uploading || restoringUploads || loading || refreshingRun || savingModel || showContext || !modelReady) return;
    const snapshot = readDraft();
    const text = input.trim() || (files.length?'请阅读我上传的附件。':'请执行所选 Skill。'); const fileIds = files.map(f => f.id); const skillIds = selectedSkillIds(selectedSkills,skills); const signature = JSON.stringify([text, fileIds,skillIds]);
    if (skillIds.length !== selectedSkills.length) { setError('所选 Skill 仍在加载或已不可用，请检查后再发送。'); return; }
    const submission = snapshot.submission?.signature === signature ? snapshot.submission
      : { signature, id: clientRequestId(), queued: Boolean(active || queue.state?.items.length) };
    setSubmission(submission);
    const sentInputVersion = inputVersion.current; assistance.invalidate();
    submissionPending.current = true;
    setSending(true); setError(''); shouldFollow.current = true;
    try {
      if (submission.queued) {
        const payload = chatMessagePayload({ requestId: submission.id, text, fileIds, skillIds, trial, autoModelRouting });
        await queue.enqueue(payload.auto_route ? payload : { ...payload, ...selection });
        finishSubmission(submission.id, snapshot);
        if (!mounted.current) return;
        if (inputVersion.current === sentInputVersion) setSkillQuery(null);
        return;
      }
      const result = await sendChatMessage(conversation.id,chatMessagePayload({requestId:submission.id,text,fileIds,skillIds,trial,autoModelRouting}));
      finishSubmission(submission.id, snapshot);
      if (!mounted.current) return;
      recall.current = -1;
      history.addAccepted(result.message); if (inputVersion.current === sentInputVersion) setSkillQuery(null);setRunStage('initializing');
      setRun({ id: result.run_id, conversation_id: conversation.id, status: 'queued', assistant_message_id: null, error: null, usage: null, approvals: [], created_at: new Date().toISOString(), completed_at: null });
      const next = await api<Run>(`/runs/${result.run_id}`); if (!mounted.current) return; applyRunReadback(next); onChangedRef.current();
    } catch (e) {
      if (e instanceof ApiError && e.status >= 400 && e.status < 500 && e.status !== 401
          && readDraft().submission?.id === submission.id) setSubmission(undefined);
      if (mounted.current) {
        setError(errorText(e));
        if (e instanceof ApiError && e.code === 'MODEL_SELECTION_REQUIRED') setModelReconnectRequired(true);
      }
    } finally { submissionPending.current = false; if (mounted.current) setSending(false); }
  };
  const queueReadVersion = useRef(0);
  const queue = useMessageQueue(conversation.id, canSend && !loading && !active && !sending && !savingModel && !showContext && !archived && !refreshingRun && modelReady, `${run?.id || ''}:${run?.status || ''}`, async runId => {
    const version = ++queueReadVersion.current;
    // Select an accepted task immediately. Later HTTP reads use the same
    // terminal-state guard as normal sends and cannot revive a finished run.
    setRun(current => current?.id === runId || current && isActive(current.status) ? current : { id: runId, conversation_id: conversation.id, status: 'queued', assistant_message_id: null, error: null, usage: null, approvals: [], created_at: new Date().toISOString(), completed_at: null });
    const [next, nextMessages] = await Promise.all([api<Run>(`/runs/${runId}`), readChatHistory(conversation.id)]);
    if (!mounted.current || version !== queueReadVersion.current) return;
    history.reconcileLatest(nextMessages); applyRunReadback(next); onChangedRef.current();
  });
  const [showMemory, setShowMemory] = useState(false), [memoryDirty, setMemoryDirty] = useState(false), [memorySaving, setMemorySaving] = useState(false), [confirmCloseMemory, setConfirmCloseMemory] = useState(false);
  const memoryDirtyChanged = useCallback((dirty:boolean) => { setMemoryDirty(dirty); onMemoryDirtyChange?.(dirty); }, [onMemoryDirtyChange]);
  const memorySavingChanged = useCallback((saving:boolean) => { setMemorySaving(saving); onMemorySavingChange?.(saving); }, [onMemorySavingChange]);
  const closeMemory = () => { if (memorySaving) return; if (memoryDirty) setConfirmCloseMemory(true); else setShowMemory(false); };
  const retryLastMessage = async () => {
    if (!run || !canSend || archived || active || sending || submissionPending.current || uploadPending.current || uploading || loading || refreshingRun || savingModel || showContext || !modelReady) return;
    // The server reconstructs the original inputs, or continues with tools
    // disabled, using the recorded effects of this exact source run.
    const signature = run.id;
    if (!retryRequest.current || retryRequest.current.signature !== signature) retryRequest.current = { signature, id: clientRequestId() };
    submissionPending.current = true;
    // Keep the previous result and its CSV available until a new run is accepted.
    // The run subscription resets events only after the run ID changes.
    assistance.invalidate(); setRetrying(true); setSending(true); setError(''); shouldFollow.current = true;
    try {
      const result = await sendChatMessage(conversation.id, recoveryMessagePayload(run.id, retryRequest.current.id));
      if (!mounted.current) return;
      history.addAccepted(result.message);
      retryRequest.current = null; setRunStage('initializing');
      setRun({ id: result.run_id, conversation_id: conversation.id, status: 'queued', assistant_message_id: null, error: null, usage: null, approvals: [], created_at: new Date().toISOString(), completed_at: null });
      const next = await api<Run>(`/runs/${result.run_id}`); if (!mounted.current) return; applyRunReadback(next); onChangedRef.current();
    } catch (e) { if (mounted.current) { setError(errorText(e)); if (e instanceof ApiError && e.code === 'MODEL_SELECTION_REQUIRED') setModelReconnectRequired(true); } }
    finally { submissionPending.current = false; if (mounted.current) { setRetrying(false); setSending(false); } }
  };
  const upload = (selectedFiles: FileList | File[] | null) => {
    if (!canUseAttachments || !selectedFiles?.length) return;
    if (archived || loading || sending || submissionPending.current) {
      setError(archived ? '恢复会话后可以添加附件。' : loading ? '会话正在加载，附件尚未添加。加载后请重试。' : '正在提交消息，附件尚未添加。请稍后重试。');
      return;
    }
    const selected = Array.from(selectedFiles);
    const runtimeType = conversation.runtime_type || 'claude';
    const binaryMaxBytes = config.chat_attachment_upload_max_bytes ?? 0;
    const useBinaryUpload = runtimeType === 'harness' && Number.isSafeInteger(binaryMaxBytes) && binaryMaxBytes > 0;
    const maxBytes = useBinaryUpload ? binaryMaxBytes : config.upload_max_bytes;
    const inlineLegacy = runtimeType === 'claude' && models.find(model => model.id === selection.model_id)?.capabilities?.includes('multimodal');
    const problem = chatAttachmentProblem(
      [...files, ...attachmentQueue.read().map(job => job.file), ...selected],
      runtimeType, maxBytes, config.chat_attachment_limits, inlineLegacy ? config.legacy_chat_image_max_bytes : undefined,
    );
    if (problem) { setError(problem); if (fileInput.current) fileInput.current.value = ''; return; }
    uploadPending.current = true;
    assistance.invalidate();
    setError('');
    if (fileInput.current) fileInput.current.value = '';
    attachmentQueue.add(selected, useBinaryUpload, saveAttachment);
  };
  const pasteUpload = useRef(upload);
  pasteUpload.current = upload;
  useEffect(() => {
    if (!canUseAttachments) return;
    const paste = (event: ClipboardEvent) => {
      if (event.defaultPrevented || document.querySelector('[role="dialog"][aria-modal="true"]')) return;
      const target = event.target instanceof Element ? event.target : document.activeElement;
      if (target !== textInput.current &&
          target?.closest('input, textarea, select, [role="textbox"], [contenteditable="true"], [contenteditable=""]')) return;
      const selected = clipboardFiles(event.clipboardData);
      if (!selected.length) return;
      if (!event.clipboardData?.getData('text/plain')) event.preventDefault();
      void pasteUpload.current(selected);
    };
    window.addEventListener('paste', paste);
    return () => window.removeEventListener('paste', paste);
  }, [canUseAttachments]);
  const removeDraftFile = async (file: StoredFile) => {
    assistance.invalidate();
    setFiles(previous => previous.filter(item => item.id !== file.id));
    if (!config.chat_attachment_discard) return;
    try {
      await remove(`/files/${encodeURIComponent(file.id)}`);
    } catch (reason) {
      if (reason instanceof ApiError && reason.code === 'UPLOAD_IN_USE') return;
      setFiles(previous => previous.some(item => item.id === file.id) ? previous : [...previous, file]);
      if (mounted.current) setError(errorText(reason));
    }
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
    selectionVersion.current += 1; setRoutingSaved(false); setSavingModel(true); setError('');
    try {
      const saved = await saveModelRouting(conversation.id, automatic);
      if (!mounted.current) return;
      setAutoModelRouting(Boolean(saved.auto_model_routing));
      setRoutingSaved(true);
      setSelection({ model_id: saved.model_id, reasoning_level: saved.reasoning_level });
      onChangedRef.current();
    } catch (error) { if (mounted.current) setError(errorText(error)); }
    finally { if (mounted.current) setSavingModel(false); }
  };
  const openRecommendation = () => { const summary = fitnessTaskSummary(input,messages,assistant?.description||''); setRecommendSummary(summary); setRecommendation(null); setRecommendOpen(true); void recommend(summary); };
  return <div className="chat-page"><header className="chat-header"><div className="chat-heading"><AiIdentity active={aiWorking}/><div><h1>{title || '新会话'}</h1><span>{assistant?.name || '业务助手'}{active && <><i/> {statusLabels[run.status]}</>}</span></div></div><div className="chat-actions">{!trial&&<button type="button" className="icon-button" title="计划与子任务" aria-label="计划与子任务" onClick={()=>setShowTasks(true)}><Clock3 size={17}/></button>}{!trial&&<ConversationExport conversationId={conversation.id}/>}{!trial&&<button type="button" className="icon-button" title="内容轨迹" aria-label="内容轨迹" onClick={()=>setShowTrajectory(true)}><ListTree size={17}/></button>}{!trial&&<button type="button" className="icon-button" title="会话记忆" aria-label="会话记忆" onClick={()=>setShowMemory(true)}><BookOpen size={17}/></button>}<button className="icon-button" title="重命名会话" aria-label="重命名会话" onClick={() => { setTitleDraft(title); setRename(true); }}><Pencil size={16}/></button>{canUseContext&&<button type="button" className="icon-button" title="上下文" aria-label="上下文" disabled={savingModel || loading} onClick={() => setShowContext(true)}><Gauge size={17}/></button>}<button className="icon-button" title="归档会话" aria-label="归档会话" disabled={active} onClick={() => setArchive(true)}><Archive size={17}/></button></div></header>
    <div className="chat-proof-bar" aria-label="会话能力状态"><span><FileText size={14}/>业务会话</span><span>{assistant?.connections.length||0} 个已配置 MCP</span><span className="run-status-pill" data-state={run?.status || 'idle'}>{run?.status === 'succeeded' ? <CheckCircle2 size={14}/> : run?.status === 'failed' ? <XCircle size={14}/> : run?.status === 'awaiting_approval' ? <ShieldCheck size={14}/> : <Clock3 size={14}/>} {run?statusLabels[run.status]:'等待开始'}</span>{run&&<RunModelIdentity run={run} models={models}/>}{connectionState === 'reconnecting' && <strong>连接恢复中</strong>}</div>
    <div className="chat-layout"><section className="conversation-column"><div className="message-scroll" ref={scroll} onScroll={() => { const el = scroll.current; if (!el) return; shouldFollow.current = el.scrollHeight - el.scrollTop - el.clientHeight < 120; setShowScroll(!shouldFollow.current); }}>
      {loading ? <Loader text="正在恢复会话…"/> : messages.length === 0 && !liveText && !showThinking ? <div className="chat-intro"><AiIdentity size="large"/><h2>向{assistant?.name || '业务助手'}提问</h2><p>{assistant?.description || '描述你的问题或目标，也可以上传相关资料。'}</p><div className="chat-intro-note"><ShieldCheck size={16}/>调用需审批的业务工具时，你会先看到操作内容。</div></div> : <div className="messages">{history.page.before && <div className="chat-history-control"><Button variant="secondary" busy={history.reading} onClick={() => readHistory('older')}>加载更早消息</Button></div>}{messages.filter(m => !(active && m.role === 'assistant' && m.run_id === run?.id) && (m.role !== 'assistant' || !!m.text || m.file_ids.length > 0 || historyResultIds.has(m.id))).map(message => {
        const subagentRunId = trial ? null : subagentRunForQuestion(message, liveSubagentQuestion, run?.id);
        return <Fragment key={message.id}>
          <article className={`message ${message.role}`} data-message-id={message.id} data-search-target={message.id === targetMessageId || undefined}>
            <div className="message-label">{message.role === 'assistant'
              ? <><AiIdentity size="inline"/>{assistant?.name || '助手'}</>
              : <>你<span>{timeText(message.created_at)}</span></>}</div>
            <div className="message-content">
              {message.role === 'assistant' ? <AnswerMarkdown text={message.text} citations={message.citations}/>
                : <p className="user-text">{run?.kind === 'compact' && message.run_id === run.id ? '请求原生上下文压缩' : message.text}</p>}
              {message.role === 'user' && !!message.skills?.length && <div className="message-skill-record">
                {message.skills.map(skill => <span key={skill.id}>@{skill.name}</span>)}
              </div>}
              {canUseAttachments && message.file_ids.length > 0 && <div className="attachment-row">
                {message.file_ids.map(id => <Attachment key={id} id={id} onPreview={setPreview}/>)}
              </div>}
              {!trial && conversation.runtime_type === 'harness' && onOpenConversation && message.role === 'assistant' && message.run_id && message.text &&
                <Button variant="ghost" disabled={active || !!forking} busy={forking === message.id} onClick={() => void forkMessage(message)}>从这条消息创建分支</Button>}
              {message.run_id && historyResultIds.has(message.id) && <HistoricalBusinessResults conversationId={conversation.id}
                runId={message.run_id} messageId={message.id} question={questionForRun(messages, message.run_id)}/>}
            </div>
            {message.id === forkBoundary && <div className="chat-fork-boundary" role="separator" aria-label="分支从这里开始"><span>分支从这里开始</span></div>}
          </article>
          {subagentRunId && <InlineSubagentRun key={subagentRunId} runId={subagentRunId}/>}
        </Fragment>;
      })}{history.page.after && <div className="chat-history-control"><Button variant="secondary" busy={history.reading} onClick={() => readHistory('newer')}>加载更新消息</Button></div>}{!history.page.after && showThinking && <div className="assistant-working" role="status">
        <AiIdentity size="inline"/><span>{assistant?.name || '助手'}正在处理</span>
        <span className="assistant-working-dots" aria-hidden="true"><i/><i/><i/></span>
      </div>}{!history.page.after && showStream && <article className="message assistant"><div className="message-label"><AiIdentity size="inline"/>{assistant?.name || '助手'}{active && <span className="typing-indicator"><i/><i/><i/></span>}</div><div className="message-content"><AnswerMarkdown text={liveText} citations={liveCitations}/></div></article>}{!history.page.after&&run&&<BusinessResults events={events} runId={run.id} status={run.status} question={questionForRun(messages,run.id)}/>}{!history.page.after && active && <LiveActivity run={run} events={events} stage={runStage} connectionState={connectionState} inlineSubagents={!!liveSubagentQuestion}/>}<div className="compaction-message-container">{!history.page.after && run?.kind === 'compact' && <div className="compaction-message"><Minimize2 size={17}/><div><strong>{compactionLabel(run)}</strong><p>历史消息与附件仍保留，可在“上下文”中查看原生统计。</p></div></div>}</div>{!history.page.after && run && !active && ['failed', 'interrupted', 'cancelled'].includes(run.status) && <RunOutcome run={run} busy={retrying} disabled={retrying || sending || refreshingRun || !modelReady} onRetry={() => void retryLastMessage()} onAdjust={() => textInput.current?.focus()} onContext={() => setShowContext(true)}/>}{!history.page.after && !trial && artifacts.length > 0 && <div className="artifact-section"><span className="eyebrow">生成的文件</span><div className="attachment-row">{artifacts.map(file => <FileChip key={file.id} file={file} onPreview={() => setPreview(file)}/>)}</div></div>}</div>}
    </div>{(showScroll || history.page.after) && <button type="button" className="scroll-latest" disabled={history.reading} onClick={returnToLatest}><ArrowDown size={15}/>回到最新消息</button>}
    <div className="composer-area composer-refined">{archived&&<div className="archive-banner"><span>此会话已归档，恢复后可以继续工作。</span><Button variant="secondary" busy={restoring} onClick={async()=>{setRestoring(true);try{await patch(`/conversations/${conversation.id}`,{archived:false});setArchived(false);onChangedRef.current();}catch(e){setError(errorText(e));}finally{setRestoring(false);}}}>恢复会话</Button></div>}<ErrorNotice message={error} onDismiss={() => setError('')}/><ErrorNotice message={history.error}/>{readbackIssue && <div role="status" className="run-outcome"><p>{readbackIssue}</p><Button variant="secondary" busy={refreshingRun} onClick={() => retryReadback.current?.()}>重新读取任务结果</Button></div>}{modelReconnectRequired && <ModelRecovery currentAvailable={modelReady} busy={active || loading || sending || savingModel || uploading || showContext || refreshingRun} pending={savingModel} onReconnect={() => void changeModel(selection, true)}/>}{!trial && active && run && <RunInteractions key={run.id} runId={run.id}/>}<PendingApprovals conversationId={conversation.id} approvals={approvals} busyId={decisionBusy} onDecision={(approval,decision,digest)=>decide(approval,decision,digest)}/>
      <MessageQueue queue={queue}/>
      <MCPAvailabilityNotice events={events}/>
      <ComposerFollowups key={assistance.followups.runId || 'pending'} value={assistance.followups} onOpen={assistance.loadFollowups} onChoose={assistance.chooseFollowup}/>
      <form ref={composerForm} className="composer fw-composer" data-working={active || sending || assistance.completion.busy || undefined} onSubmit={e => { e.preventDefault(); void send(); }} onClickCapture={e => { if ((e.target as Element).closest('.chat-model-settings > summary')) { setSkillQuery(null); mention.current = null; } }}
        onDragOver={e => {
          if (!e.dataTransfer.types.includes('Files')) return;
          e.preventDefault();
          e.dataTransfer.dropEffect = canUseAttachments && !archived && !loading && !sending && !uploading ? 'copy' : 'none';
        }}
        onDrop={e => {
          if (!e.dataTransfer.files.length) return;
          e.preventDefault();
          void upload(e.dataTransfer.files);
        }}>
        {canUseSkills && skillQuery !== null && <SkillPicker skills={skills} selected={selectedSkills} query={skillQuery} highlighted={skillHighlight} onSelect={addSkill} onClose={() => { setSkillQuery(null); mention.current = null; textInput.current?.focus(); }}/>}
        {canUseSkills && selectedSkills.length > 0 && <SkillChips selected={selectedSkills} skills={skills} onRemove={id => { assistance.invalidate(); setSelectedSkills(prev => prev.filter(item => item !== id)); }} disabled={sending}/>}
        {canUseAttachments && files.length > 0 && <div className="attachment-row composer-files">{files.map(file => <FileChip key={file.id} file={file} onPreview={() => setPreview(file)} onRemove={() => void removeDraftFile(file)}/>)}</div>}
        {canUseAttachments && restoringUploads && <small role="status">正在恢复未完成的上传…</small>}
        {canUseAttachments && restorationError && <small role="status">{restorationError}</small>}
        {canUseAttachments && attachmentJobs.length > 0 && <ul className="attachment-row composer-uploads" aria-label="附件上传">{attachmentJobs.map(job =>
          <li key={job.id} className="file-chip composer-upload">
            <span>{job.file.name}</span>
            {job.status === 'uploading' && <progress aria-label={`上传 ${job.file.name}`} value={job.progress} max={1}/>}
            {job.status === 'queued' && <small>等待上传</small>}
            {job.error && <small role="alert">{job.error} 请重试或移除。</small>}
            {job.storageWarning && <small role="status">浏览器空间不足，刷新前请等待此文件上传完成。</small>}
            {job.status === 'failed' && <button type="button" onClick={() => attachmentQueue.retry(job.id)}>重试上传</button>}
            <button type="button" aria-label={`${job.status === 'failed' ? '移除' : '取消上传'} ${job.file.name}`} onClick={() => attachmentQueue.cancel(job.id)}><X size={14}/></button>
          </li>)}</ul>}
        <textarea ref={textInput} aria-label="输入消息" aria-describedby={assistance.inlineSuggestion ? 'composer-shortcuts composer-inline-suggestion' : 'composer-shortcuts'} placeholder={active ? '输入下一条问题，加入队列…' : !input ? assistance.inlineSuggestion || '使用 Flint Work' : undefined} value={input}
          onChange={e => changeDraft(e.target.value, e.target.selectionStart)}
          onCompositionStart={() => { composing.current = true; assistance.invalidate(); }} onCompositionEnd={() => { composing.current = false; }}
          onKeyDown={e => {
            const inComposition = composing.current || e.nativeEvent.isComposing || e.nativeEvent.keyCode === 229;
            if (inComposition) return;
            if (e.key === 'Enter' && !e.shiftKey && !e.ctrlKey && !e.metaKey && !e.altKey && slash.length === 1) {
              e.preventDefault(); runCommand(slash[0].open); return;
            }
            if (skillQuery !== null && ['ArrowDown', 'ArrowUp', 'Enter'].includes(e.key)) {
              e.preventDefault();
              if (e.key === 'Enter') { const choice = matchingSkills[skillHighlight]; if (choice) addSkill(choice); else setSkillQuery(null); }
              else if (matchingSkills.length) setSkillHighlight(previous => (previous + (e.key === 'ArrowDown' ? 1 : -1) + matchingSkills.length) % matchingSkills.length);
              return;
            }
            if (e.key === 'Escape') {
              if (skillQuery !== null) { e.preventDefault(); setSkillQuery(null); mention.current = null; }
              else if (assistance.completion.busy || assistance.completion.suggestion || assistance.completion.notice || assistance.inlineSuggestion) { e.preventDefault(); assistance.dismiss(); }
              return;
            }
            if (!e.shiftKey && !e.ctrlKey && !e.metaKey && !e.altKey && (e.key === 'ArrowUp' || e.key === 'ArrowDown') && recallDraft(e.key)) {
              e.preventDefault(); return;
            }
            const action = composerShortcut({ key: e.key, shiftKey: e.shiftKey, ctrlKey: e.ctrlKey, metaKey: e.metaKey, altKey: e.altKey, repeat: e.repeat, isComposing: e.nativeEvent.isComposing, keyCode: e.nativeEvent.keyCode }, { composing: composing.current, draft: input, canComplete: canUseCompletion && !archived && !sending && !loading && !savingModel && !showContext && canSend, completionBusy: assistance.completion.busy, suggestion: !!(assistance.completion.suggestion || assistance.inlineSuggestion) && e.currentTarget.selectionStart === input.length && e.currentTarget.selectionEnd === input.length, skillsOpen: skillQuery !== null });
            if (action !== 'none') { e.preventDefault(); if (action === 'complete') void assistance.complete(); else if (action === 'accept') { if (assistance.completion.suggestion) assistance.accept(); else assistance.acceptSuggestion(); } else void send(); }
          }} rows={1}/>
        {assistance.inlineSuggestion && !slash.length && <span className="composer-inline-suggestion" id="composer-inline-suggestion">
          {input && <span>{assistance.inlineSuggestion.slice(input.length)}</span>}<kbd>Tab</kbd> 采用建议
        </span>}
        {slash.length > 0 && <div className="composer-commands" aria-label="会话命令">{slash.map(command => <Button key={command.name} variant="ghost" onClick={() => runCommand(command.open)}>{command.name}</Button>)}</div>}
        <ComposerCompletion value={assistance.completion} onAccept={assistance.accept} onDismiss={assistance.dismiss}/>
        <div className="composer-toolbar composer-actions">
          <div className="composer-utilities">
            {canUseAttachments && <><input ref={fileInput} type="file" multiple hidden accept={CHAT_ATTACHMENT_ACCEPT} onChange={e => void upload(e.target.files)}/><button className="attach-button composer-icon" type="button" aria-label={uploading ? '正在上传附件' : '添加附件'} title="添加附件，也可粘贴或拖放文件到输入框" disabled={uploading || sending || loading || archived} onClick={() => fileInput.current?.click()}>{uploading ? <LoaderCircle className="spin" size={18}/> : <Paperclip size={18}/>}</button></>}
            {canUseSkills && <button type="button" className="attach-button composer-icon" aria-label="选择 Skill" title="选择 Skill" aria-expanded={skillQuery !== null} disabled={sending || loading || archived} onClick={() => { closeModelSettings(); mention.current = null; setSkillQuery(previous => previous === null ? '' : null); }}><Sparkles size={17}/></button>}
          </div>
          <ModelSettingsDisclosure automatic={autoModelRouting} selection={selection} models={models} loading={loading||modelsLoading} pending={savingModel}>
            {!trial&&<ModelRoutingControl automatic={autoModelRouting} confirmation={routingSaved ? '选模方式已保存' : undefined} disabled={archived||active||loading||sending||uploading||showContext||refreshingRun||savingModel||recommendBusy} onChange={value=>void changeRouting(value)}/>}
            {!autoModelRouting && <ModelSelector models={models} value={selection} hasHistory={hasHistory} loading={modelsLoading}
              disabled={archived || active || loading || sending || uploading || showContext || refreshingRun}
              disabledReason={archived ? '恢复此会话后可以切换模型。' : active ? '当前任务完成或停止后可以切换模型。' : showContext ? '关闭上下文窗口后可以切换模型。' : '正在读取或保存会话，请稍候再切换模型。'}
              pending={savingModel} onRecommend={canRecommend ? openRecommendation : undefined} recommendBusy={recommendBusy}
              onNewConversation={onNewConversation ? next => { closeModelSettings(); onNewConversation(next, input); } : undefined}
              onChange={next => void changeModel(next)}/> }
          </ModelSettingsDisclosure>
          {!trial && <SessionPermissionMode key={conversation.id} conversation={conversation} disabled={active || archived || loading || sending || refreshingRun}/>}
          {canUseCompletion && !assistance.inlineSuggestion && <span className="composer-input-hint"><kbd>Tab</kbd> 补全</span>}<span className="composer-action-space"/>
          {canSteer && <Button variant="secondary" onClick={() => void steer()} title="追加文字到正在执行的这轮任务，发送按钮仍用于下一轮" disabled={!input.trim() || input.trim().length > 4000 || !!files.length || !!selectedSkills.length || sending || uploading || failedAttachments}>追加到本轮</Button>}
          {active && <Button type="button" variant="secondary" busy={stopping} onClick={() => void stop()} disabled={run.status === 'cancelling'}><Square size={13}/>{run.status === 'cancelling' ? '停止中' : '停止'}</Button>}
          <button className="send-button" type="submit" aria-label={active || queue.state?.items.length ? '加入消息队列' : '发送消息'} title={failedAttachments ? '请重试或移除上传失败的附件' : active || queue.state?.items.length ? '加入消息队列' : '发送消息'} disabled={!canSend || archived || sending || uploading || restoringUploads || failedAttachments || loading || refreshingRun || savingModel || showContext || !modelReady || (!input.trim() && !files.length && !selectedSkills.length)}>{sending ? <LoaderCircle size={18} className="spin"/> : <ArrowUp size={20}/>}</button>
        </div>
      </form>
      {steering && <div className="composer-steering" role="status">
        {steering.status === 'applied' ? '追加指令已进入本轮。' : steering.status === 'queued' || steering.status === 'pending' ? '等待本轮接收追加指令。' : steering.status === 'rejected' ? '本轮未接收追加指令，文字仍保存在草稿中。' : '未确认本轮是否收到这条追加指令。'}
        {steering.status === 'uncertain' && !input.trim() && <Button variant="ghost" onClick={() => fillDraft(steering.text)}>放回草稿</Button>}
      </div>}
      <div className="composer-footnote"><span id="composer-shortcuts"><kbd>Enter</kbd> 发送 · <kbd>Shift + Enter</kbd> 换行</span><span>开物的回答也可能出错</span></div>
    </div></section>
    {showReferences && <ComposerReferences messages={messages} fileIds={canUseAttachments ? [...new Set(messages.flatMap(message => message.file_ids))] : []}
      onMessage={message => insertReference(`> ${message.text.trim().slice(0, 4000).replaceAll('\n', '\n> ')}\n\n`)}
      onFile={referenceFile} onClose={() => setShowReferences(false)}/>}
    {showTasks&&<ConversationTasks conversationId={conversation.id} runId={run?.id} canDelegate={!!config.harness_subagents && canSteer} onSummarize={results => {
      const summary = `请汇总以下子任务结果，并注明尚未验证的结论。\n\n${results}`;
      const next = input.trim() ? `${input.trimEnd()}\n\n${summary}` : summary;
      if (next.length > 50000) { setError('子任务结果无法加入当前草稿，输入会超过 50000 字。'); }
      else fillDraft(next);
      setShowTasks(false);
      requestAnimationFrame(() => textInput.current?.focus());
    }} onClose={()=>setShowTasks(false)}/>}
    {showTrajectory&&<SessionTrajectory conversationId={conversation.id} title={title} onClose={()=>setShowTrajectory(false)}/>}
    {showMemory && <Modal title="会话记忆" wide onClose={closeMemory}><div className="modal-body conversation-memory-panel"><MemoryWorkspace conversation={conversation} onDirtyChange={memoryDirtyChanged} onSavingChange={memorySavingChanged}/></div></Modal>}
    {confirmCloseMemory && <Modal title="放弃未保存的记忆修改" onClose={()=>setConfirmCloseMemory(false)}><div className="modal-body"><p>未保存的草稿将丢失。已经发送的修改仍会继续处理。</p></div><footer className="modal-footer"><Button variant="secondary" onClick={()=>setConfirmCloseMemory(false)}>继续编辑</Button><Button variant="danger" disabled={memorySaving} onClick={()=>{setConfirmCloseMemory(false);setShowMemory(false);}}>放弃修改并关闭</Button></footer></Modal>}

    </div>{!trial && showContext && <ContextDialog conversationId={conversation.id} models={models} busy={active || sending || uploading || savingModel || loading} run={run} onClose={() => setShowContext(false)} onCompact={result => { setShowContext(false); history.addAccepted(result.message); setRun({ id: result.run_id, conversation_id: conversation.id, status: 'queued', kind: 'compact', compaction: null, assistant_message_id: null, error: null, usage: null, approvals: [], created_at: new Date().toISOString(), completed_at: null }); void api<Run>(`/runs/${result.run_id}`).then(applyRunReadback).catch(e => { if (mounted.current) setError(errorText(e)); }); onChangedRef.current(); }}/>} {preview && <FilePreview key={preview.id} file={preview} onClose={() => setPreview(null)}/>}{rename && <Modal title="重命名会话" onClose={() => { if (!savingTitle) setRename(false); }}><form onSubmit={e => { e.preventDefault(); void saveTitle(); }}><div className="modal-body"><Field label="会话名称"><input value={titleDraft} onChange={e => setTitleDraft(e.target.value)} disabled={savingTitle} maxLength={200} required autoFocus/></Field></div><footer className="modal-footer"><Button type="button" variant="secondary" disabled={savingTitle} onClick={() => setRename(false)}>取消</Button><Button busy={savingTitle} disabled={!titleDraft.trim()} type="submit">保存名称</Button></footer></form></Modal>}{archive && <Modal title="归档此会话" onClose={() => setArchive(false)}><div className="modal-body"><p>归档后，会话会从最近列表中移除。会话和附件仍保存在服务端。</p></div><footer className="modal-footer"><Button variant="secondary" onClick={() => setArchive(false)}>取消</Button><Button onClick={async () => { try { await patch(`/conversations/${conversation.id}`, { archived: true }); onChangedRef.current(); onArchived(); } catch (e) { setError(errorText(e)); setArchive(false); } }}>归档会话</Button></footer></Modal>}{recommendOpen && <Modal title="为当前任务推荐模型" onClose={() => { if (!recommendBusy) setRecommendOpen(false); }}><div className="modal-body"><p>根据当前问题、最近消息和助手职责推荐模型，只使用任务摘要。推荐生效后将固定使用该模型，可随时改回自动选择。</p><div className="fitness-context" role="status"><span className="eyebrow">当前会话上下文</span><p>{recommendSummary}</p></div>{recommendation && <div className="recommendation-result"><strong>推荐模型：{recommendation.model_label} · {recommendation.reasoning_level}</strong><p>{recommendation.reason}</p><small>路由器：{recommendation.router.model_id} · 输入 {recommendation.router.input_chars} 字符 · {recommendation.router.mode === 'provider' || recommendation.router.mode === 'provider_fallback' ? '已使用授权模型路由' : recommendation.router.target_status === 'unavailable' ? `目标线路不可用（${recommendation.router.target_model_id}）` : '本地备用规则'}</small></div>}</div><footer className="modal-footer"><Button type="button" variant="secondary" disabled={recommendBusy} onClick={() => setRecommendOpen(false)}>{recommendation ? '完成' : '关闭'}</Button></footer></Modal>}</div>;
}

export function LiveActivity({ run, events, stage, connectionState, inlineSubagents = false }: { run: Run; events: StreamEvent[]; stage: string; connectionState: 'connected' | 'reconnecting'; inlineSubagents?: boolean }) {
  const toolFinished = new Set(events.filter(event => event.type === 'tool.completed').map(event => String(event.data.tool_call_id || event.id)));
  const pendingTool = [...events].reverse().find(event => event.type === 'tool.started' && !toolFinished.has(String(event.data.tool_call_id || event.id)));
  const latestTool = [...events].reverse().find(event => event.type === 'tool.completed');
  const latestStage = [...events].reverse().find(event => event.type === 'run.stage' && event.data.stage === stage);
  const latestApproval = [...events].reverse().find(event => event.type === 'approval.required' || event.type === 'subagent.approval.required');
  const latestCompaction = [...events].reverse().find(event => event.type.startsWith('context.compaction'));
  let title = '正在处理当前任务';
  let detail = '可在轨迹中查看处理进度。';
  let icon = <LoaderCircle className="spin" size={16}/>;
  let waiting = false;
  if (connectionState === 'reconnecting') {
    title = '连接暂时中断'; detail = '正在恢复连接，不会重复发起已经记录的工具调用。'; icon = <Radio size={16}/>;
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
  } else if (['scoping', 'routing', 'evidence', 'preparing_attachments', 'delivering_attachments'].includes(stage)) {
    const presentation = latestStage ? evidenceStagePresentation(latestStage, events) : runStagePresentation({ stage });
    title = presentation.label; detail = presentation.detail;
    icon = presentation.confirmed ? <CheckCircle2 size={16}/> : <Circle size={16}/>;
  } else if (stage === 'initializing' || run.status === 'queued') {
    title = '正在准备执行环境'; detail = '确认助手配置、会话上下文和可用能力。';
  } else if (stage === 'runtime_ready') {
    title = '执行环境已就绪'; detail = '即将向模型提交当前任务。'; icon = <CheckCircle2 size={16}/>;
  } else if (stage === 'model_started') {
    title = '模型已开始响应'; detail = '正在整理回答，稍后将显示内容。';
  } else if (stage === 'answering' || events.some(event => event.type === 'message.delta')) {
    title = '正在生成回答'; detail = '回答将逐步显示，可在轨迹中查看工具与来源。';
  }
  const visible = [...events].filter(event => ['run.stage', 'tool.started', 'tool.completed', 'approval.required', 'approval.resolved', 'approval.model_decided', 'approval.session_decided', 'approval.dual_vote', 'memory.pending', 'memory.updated', 'memory.aborted', 'subagent.plan.created', 'subagent.started', 'subagent.progress', 'subagent.completed', 'subagent.failed', 'subagent.cancelled', 'subagent.plan.completed', 'subagent.approval.required', 'subagent.approval.denied', 'subagent.approval.resolved', 'context.compaction_started', 'context.compacted'].includes(event.type)).filter(event =>
    !inlineSubagents || !event.type.startsWith('subagent.') || event.type.startsWith('subagent.approval.')
  ).slice(-4);
  const eventLabel = (event: StreamEvent) => {
    if (event.type === 'run.stage') return evidenceStagePresentation(event, events).label;
    if (event.type === 'memory.pending') return '记忆已暂存，等待任务成功后保存';
    if (event.type === 'memory.updated') return '记忆已正式保存';
    if (event.type === 'memory.aborted') return '记忆未保存，暂存已废弃';
    if (event.type === 'tool.started') return `调用 ${toolLabel(String(event.data.tool_name || '业务工具'))}`;
    if (event.type === 'tool.completed') return `${toolResultLabels[toolResultState(event)]} · ${toolLabel(String(event.data.tool_name || '业务工具'))}`;
    if (event.type === 'approval.required') return '等待操作确认';
    if (event.type === 'approval.resolved') return event.data.decision === 'allow' ? '已允许操作' : '未获授权';
    if (event.type === 'approval.model_decided') return event.data.decision === 'allow' ? '模型代审允许低风险操作' : '模型代审拒绝操作';
    if (event.type === 'approval.session_decided') return event.data.mode === 'full' ? '会话完全权限已允许操作' : '会话自动审批已允许操作';
    if (event.type === 'approval.dual_vote') return `双人审批投票 ${String(event.data.votes || 0)}/${String(event.data.required || 2)}`;
    if (event.type === 'subagent.plan.created') return `已拆分 ${Array.isArray(event.data.children) ? event.data.children.length : '若干'} 个子任务`;
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

export function PendingApprovals({ conversationId, approvals, busyId, onDecision }: { conversationId:string; approvals:Approval[]; busyId:string; onDecision:(approval:Approval,decision:'allow'|'deny',digest?:string)=>Promise<boolean> }) {
  if (!approvals.length) return null;
  return <section aria-label="待确认操作" style={{maxHeight:'40vh',overflowY:'auto'}}><p className="section-help">有 {approvals.length} 项操作等待你的确认</p>{approvals.map(approval => approval.tool_name===MEMORY_WRITE_TOOL?<MemoryApprovalCard key={approval.id} conversationId={conversationId} approval={approval} busy={busyId===approval.id} onDecision={(decision,digest)=>onDecision(approval,decision,digest)}/>:<ApprovalCard key={approval.id} approval={approval} busy={busyId===approval.id} onDecision={decision=>onDecision(approval,decision)}/>)}</section>;
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
  if (stage === 'preparing_attachments') return { label: '正在准备附件', detail: '检查原件并转换模型可读内容。', confirmed: false };
  if (stage === 'delivering_attachments') return { label: '正在载入附件', detail: '模型开始处理后会在这里显示进度。', confirmed: false };
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

const isWebRead = (name: unknown) => name === 'mcp__workspace__web_read' || name === 'web_read';
const isSearchCandidate = (name: unknown) => ['WebSearch', 'web_search', 'mcp__workspace__web_search', 'image_search', 'mcp__workspace__image_search'].includes(String(name));

/** Registered search candidates do not establish that page text was read. */
export function webReadEvidenceSummary(events: StreamEvent[], runId: string) {
  const completed = events.filter(event => event.run_id === runId && event.type === 'tool.completed').sort((a, b) => a.id - b.id);
  const reads = completed.filter(event => isWebRead(event.data.tool_name));
  const failures = reads.filter(event => event.data.success === false);
  if (!failures.length) return null;
  const sources = new Set<string>(); let countedOnly = 0;
  for (const event of completed) {
    if (event.data.success !== true || isSearchCandidate(event.data.tool_name)) continue;
    if (Array.isArray(event.data.source_ids)) {
      event.data.source_ids.forEach(id => { if (typeof id === 'string' && id.trim()) sources.add(id); });
    } else { const count = reportedSourceCount(event); if (typeof count === 'number' && Number.isSafeInteger(count)) countedOnly = Math.max(countedOnly, count); }
  }
  const count = Math.max(sources.size, countedOnly);
  if (!count && reads.at(-1)?.data.success !== false) return null;
  // The host emits a bounded public error; never inspect tool responses, URLs,
  // input arguments or raw exceptions for a guessed reason.
  const failure = failures.at(-1)!;
  const reason = typeof failure.data.error_message === 'string' && failure.data.error_message.trim()
    ? failure.data.error_message.trim().slice(0, 300) : '本次网页读取失败，具体原因未提供。';
  return {
    label: count ? '部分网页读取未完成' : '网页读取未完成',
    detail: count ? `本轮已登记${countedOnly ? '至少' : ''} ${count} 个来源，内容仍需核对。${reason}` : `${reason} 尚未取得可核对的正文来源；搜索结果不能代替网页正文。`,
    confirmed: false,
  };
}

/** Bind the evidence stage to its adjacent completion, never a later attempt. */
export function evidenceStagePresentation(event: StreamEvent, events: StreamEvent[]) {
  const fallback = runStagePresentation(event.data);
  if (event.data.stage !== 'evidence' || !isWebRead(event.data.tool_name)) return fallback;
  const following = events.filter(item => item.run_id === event.run_id && item.id > event.id).sort((a, b) => a.id - b.id);
  const boundary = following.find(item => item.type === 'tool.completed' || (item.type === 'run.stage' && item.data.stage === 'evidence'));
  if (!boundary || boundary.type !== 'tool.completed' || boundary.data.tool_name !== event.data.tool_name) return fallback;
  return webReadEvidenceSummary(events.filter(item => item.id <= boundary.id), event.run_id) || fallback;
}

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
  const webSummary = webReadEvidenceSummary(events, run.id);
  if (!evidences.length) return <section className="execution-evidence" aria-label="范围与证据状态"><div className="execution-evidence-heading"><div><span className="eyebrow">范围与证据</span><h3>等待工具返回后展示</h3></div><small>未返回的字段显示“未提供”</small></div><p className="section-help">当前还没有可核验的业务工具结果。</p></section>;
  return <section className="execution-evidence" aria-label="范围与证据状态"><div className="execution-evidence-heading"><div><span className="eyebrow">范围与证据</span><h3>工具返回的范围与来源</h3></div><small>未返回的字段显示“未提供” · {evidences.length} 次调用</small></div>{webSummary&&<p className="execution-evidence-note" role="status"><strong>{webSummary.label}</strong> {webSummary.detail}</p>}{evidences.map(evidence => { const cells: Array<[string, string]> = [
    ['业务域', evidence.domain], ['流程', evidence.process], ['对象', evidence.object], ['指标', evidence.metric],
    ['时间范围', evidence.timeRange], ['筛选条件', evidence.filters], ['返回行数', evidence.returnedRows], ['原始行数', evidence.originalRows],
  ]; return <div className="execution-evidence-tool" key={evidence.id}><div className="execution-evidence-tool-title"><strong>{evidence.toolName}</strong><span className={`badge ${evidence.status === 'succeeded' ? 'green' : evidence.status === 'failed' ? 'amber' : ''}`}>{toolResultLabels[evidence.status]}</span></div>{evidence.status === 'succeeded' ? <><dl className="execution-evidence-grid">{cells.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl><div className="execution-evidence-meta"><span>来源 {evidence.sourceCount}</span><span>缓存 {evidence.cache}</span></div>{evidence.modelInputBytes!==unknownFact&&<p className="execution-evidence-note">提供给模型的数据 {evidence.modelInputBytes} 字节（UTF-8，非 Token）</p>}</> : <p className="execution-evidence-note">{evidence.status === 'failed' ? evidence.error || '本次调用失败，未展示业务结果。' : '调用结果尚未确认，业务范围与来源保持未提供。'}</p>}</div>; })}</section>;
}

export function EventRow({ event, events = [] }: { event: StreamEvent; events?: StreamEvent[] }) {
  let label = ''; let detail = ''; let icon = <Circle size={14}/>;
  if (event.type === 'run.started') { label = '开始执行'; icon = <Radio size={14}/>; }
  if (event.type === 'run.stage') { const presentation = evidenceStagePresentation(event, events); label = presentation.label; detail = presentation.detail; icon = presentation.confirmed ? <CheckCircle2 size={14}/> : <Clock3 size={14}/>; }
  if (event.type === 'tool.started') { label = toolLabel(String(event.data.tool_name || '调用工具')); icon = <ArrowUpRightIcon/>; }
  if (event.type === 'tool.completed') { const state = toolResultState(event); const sourceCount = reportedSourceCount(event); label = `${toolResultLabels[state]} · ${toolLabel(String(event.data.tool_name || '工具调用'))}`; detail = state === 'failed' ? (typeof event.data.error_message==='string' ? event.data.error_message : '未取得成功的工具结果。') : state === 'unknown' ? '未收到明确的调用结果，暂不报告业务结果。' : typeof sourceCount === 'number' && sourceCount > 0 ? `已登记 ${sourceCount} 个来源，内容仍需核对。` : '未提供来源。'; icon = state === 'succeeded' ? <CheckCircle2 size={14}/> : state === 'failed' ? <XCircle size={14}/> : <Circle size={14}/>; }
  if (event.type === 'approval.resolved') { label = event.data.decision === 'allow' ? '已允许操作' : '已拒绝操作'; const source = String(event.data.decision_source || ''); detail = source && source !== 'user' ? `决策来源：${source === 'timeout' ? '超时' : source === 'cancelled' ? '任务取消' : source === 'restart' ? '服务重启' : source === 'interrupted' ? '执行中断' : '运行结束'}` : ''; icon = <ShieldCheck size={14}/>; }
  if (event.type === 'approval.session_decided') { label = event.data.mode === 'full' ? '会话完全权限已允许操作' : '会话自动审批已允许操作'; detail = String(event.data.reason || ''); icon = <ShieldCheck size={14}/>; }
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
  return <section className="run-metrics" aria-label="本次运行用量"><dl>{metadata&&<><dt>执行方式</dt><dd>模型信息查询</dd><dt>模型调用</dt><dd>未调用模型</dd></>}<dt>{measuredTotal !== null ? '总耗时（含等待）' : '已用时间（按时间差）'}</dt><dd>{duration}</dd>{run.timings&&Object.entries({first_server_progress_ms:'服务端首个进度',first_tool_attempt_ms:'首个工具调用尝试',first_tool_success_ms:'首个工具成功返回',runtime_ready_ms:'运行环境就绪',first_model_event_ms:'首个模型事件',first_text_ms:'首段正文'}).map(([key,label])=>{const value=run.timings?.[key as keyof typeof run.timings];return typeof value==='number'?<div className="timing-row" key={key}><dt>{label}</dt><dd>{(value/1000).toFixed(2)} 秒</dd></div>:null;})}<dt>输入 Token</dt><dd>{tokens('input_tokens')}</dd><dt>输出 Token</dt><dd>{tokens('output_tokens')}</dd><dt>缓存读取 Token</dt><dd>{tokens('cache_read_input_tokens')}</dd><dt>费用估算</dt><dd>{costText}</dd>{stages.length>0&&<><dt>Token 阶段</dt><dd><div className="token-stage-list">{stages.map((stage,index)=><span key={`${String(stage.stage||'stage')}-${index}`}><strong>{String(stage.stage||'未知')}</strong> · {stage.source === 'harness_provider' ? '标准引擎用量' : stage.source === 'claude_sdk' ? '兼容引擎用量' : '用量回执'}<small>{stageTokens(stage)}</small></span>)}</div></dd></>}</dl><a className="trace-download" href={`${API_BASE}/runs/${encodeURIComponent(run.id)}/trace`} download={`run-${run.id}.json`}><Download size={14}/>下载轨迹 JSON</a></section>;
}
