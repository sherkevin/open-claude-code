import { useEffect, useRef, useState } from 'react';
import { ArrowUpLeft, Check, ChevronDown, LoaderCircle, Sparkles, X } from 'lucide-react';
import { api } from './api';
import { isActive, type Message, type Run } from './types';

export const composerInputMaxHeight = (viewportHeight: number) => Math.min(144, Math.max(72, Math.round(viewportHeight * .18)));

type ShortcutEvent = { key: string; shiftKey?: boolean; ctrlKey?: boolean; metaKey?: boolean; altKey?: boolean; repeat?: boolean; isComposing?: boolean; keyCode?: number };
export function composerShortcut(event: ShortcutEvent, state: { composing: boolean; draft: string; canComplete: boolean; completionBusy: boolean; suggestion: boolean; skillsOpen: boolean }) {
  if (state.composing || event.isComposing || event.keyCode === 229) return 'none';
  if (event.key === 'Tab' && !event.shiftKey && !event.ctrlKey && !event.metaKey && !event.altKey && !event.repeat && state.canComplete && !state.completionBusy && !state.skillsOpen) {
    if (state.suggestion) return 'accept';
    if (state.draft.trim()) return 'complete';
  }
  if (event.key === 'Enter' && !event.shiftKey && !event.altKey && !state.skillsOpen && !event.repeat) return 'send';
  return 'none';
}

export function completedAnswerRun(conversationId: string, run: Run | null, messages: Message[]) {
  const last = messages.at(-1);
  return run?.conversation_id === conversationId && run.status === 'succeeded' && run.kind !== 'compact' && last?.role === 'assistant' && last.run_id === run.id && !!last.text.trim() ? run.id : null;
}

export function followupTexts(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return [...new Set(value.filter((item): item is string => typeof item === 'string').map(item => item.trim()).filter(item => item.length > 0 && item.length <= 160 && !/[\r\n]/.test(item)))].slice(0, 3);
}

type Completion = { busy: boolean; suggestion: string; notice: string };
type Followups = { busy: boolean; suggestions: string[]; notice: string; runId: string | null };
const emptyCompletion: Completion = { busy: false, suggestion: '', notice: '' };
const emptyFollowups: Followups = { busy: false, suggestions: [], notice: '', runId: null };
export const COMPLETION_TIMEOUT_MS = 20_000;
const shortPrompts: Record<string, string> = {
  查: '查询最近的订单，并列出待处理事项。',
  你: '你可以帮我完成哪些工作？',
  请: '请帮我查找相关业务资料，并注明来源。',
  帮: '帮我整理一份待办清单。',
  如: '如何查询最近的订单和交付状态？',
  我: '我想了解目前有哪些业务数据可以查。',
  看: '看一下近期的库存变化，指出异常之处。',
};
const starterQuestion = (assistantName?: string) => assistantName?.includes('工业')
  ? '最近有哪些订单需要跟进？'
  : '你可以帮我完成哪些工作？';

export function completionSuggestion(draft: string, value: unknown): string {
  return typeof value === 'string' && value.startsWith(draft) && value.slice(draft.length).trim() && [...value].length <= 700 ? value : '';
}
export function completionNotice(mode?: string): string {
  if (mode === 'disabled') return '补全已关闭，可以直接发送。';
  if (mode === 'unavailable') return '尚未配置可用的补全模型，请联系管理员。';
  if (mode === 'provider_unconfigured' || mode === 'provider_invalid') return '补全服务尚未就绪，请联系管理员。';
  if (mode === 'budget_disabled') return '补全调用尚未启用，请联系管理员。';
  if (mode === 'input_too_long') return 'Tab 补全支持 500 字符以内的草稿，当前内容可以直接发送。';
  if (mode === 'provider_response_rejected') return '未获得有效补全，可以继续输入或直接发送。';
  return '暂时没有更合适的补全。';
}
type Options = {
  conversationId: string; draft: string; hasExtraDraft: boolean; disabled: boolean; settled: boolean;
  completionEnabled: boolean; followupsEnabled: boolean; run: Run | null; messages: Message[];
  assistantName?: string; onDraft: (value: string) => void;
};

/** Auxiliary requests never submit messages or replace a draft without acceptance. */
export function useComposerAssistance(options: Options) {
  const latest = useRef(options); latest.current = options;
  const revision = useRef(0);
  const mounted = useRef(true);
  const completionRequest = useRef<AbortController | null>(null);
  const followupRequest = useRef<AbortController | null>(null);
  const acceptedOrigin = useRef<{ scope: string; draft: string; revision: number } | null>(null);
  const observedActive = useRef(new Set<string>());
  const requestedFollowups = useRef(new Set<string>());
  const [completion, setCompletion] = useState<Completion>(emptyCompletion);
  const [followups, setFollowups] = useState<Followups>(emptyFollowups);
  const [previewDismissed, setPreviewDismissed] = useState(false);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; completionRequest.current?.abort(); followupRequest.current?.abort(); };
  }, []);

  const invalidate = () => {
    revision.current += 1;
    completionRequest.current?.abort(); completionRequest.current = null;
    followupRequest.current?.abort(); followupRequest.current = null;
    acceptedOrigin.current = null;
    setCompletion(emptyCompletion); setFollowups(emptyFollowups);
  };
  useEffect(() => { invalidate(); setPreviewDismissed(false); }, [options.conversationId]);
  useEffect(() => { setPreviewDismissed(false); }, [options.draft, options.run?.id]);
  // A model/settings dialog or send can make the original draft ineligible.
  useEffect(() => { if (options.disabled) invalidate(); }, [options.disabled]);
  useEffect(() => {
    if (options.completionEnabled) return;
    completionRequest.current?.abort(); completionRequest.current = null;
    acceptedOrigin.current = null; setCompletion(emptyCompletion);
  }, [options.completionEnabled]);
  useEffect(() => {
    if (options.followupsEnabled) return;
    followupRequest.current?.abort(); followupRequest.current = null;
    setFollowups(emptyFollowups);
  }, [options.followupsEnabled]);

  const complete = async () => {
    const start = latest.current;
    if (!mounted.current || start.disabled || !start.completionEnabled || !start.draft.trim() || completionRequest.current) return;
    acceptedOrigin.current = null;
    if ([...start.draft].length > 500) {
      setCompletion({ busy: false, suggestion: '', notice: completionNotice('input_too_long') });
      return;
    }
    const controller = new AbortController(); completionRequest.current = controller;
    const singleCharacter = Array.from(start.draft.trim()).length === 1;
    const token = revision.current;
    const current = () => mounted.current && !controller.signal.aborted && completionRequest.current === controller && revision.current === token && latest.current.conversationId === start.conversationId && latest.current.draft === start.draft && latest.current.completionEnabled && !latest.current.disabled;
    setCompletion({ busy: true, suggestion: '', notice: '' });
    const timeout = window.setTimeout(() => {
      if (!current()) return;
      controller.abort(); completionRequest.current = null;
      setCompletion({ busy: false, suggestion: '', notice: singleCharacter ? '' : '补全等待超时，可以重试或直接发送。' });
    }, COMPLETION_TIMEOUT_MS);
    try {
      const result = await api<{ suggestion: string | null; router: { mode: string } }>('/models/complete', { method: 'POST', body: JSON.stringify({ text: start.draft }), signal: controller.signal });
      if (!current()) return;
      const suggestion = completionSuggestion(start.draft, result?.suggestion);
      if (suggestion) {
        acceptedOrigin.current = { scope: start.conversationId, draft: start.draft, revision: token };
        setCompletion({ busy: false, suggestion, notice: '' });
      } else {
        setCompletion({ busy: false, suggestion: '', notice: singleCharacter ? '' : completionNotice(result?.router?.mode) });
      }
    } catch {
      if (current()) setCompletion({ busy: false, suggestion: '', notice: singleCharacter ? '' : '补全暂不可用，可以继续输入。' });
    } finally {
      window.clearTimeout(timeout);
      if (completionRequest.current === controller) completionRequest.current = null;
    }
  };
  const accept = () => {
    const origin = acceptedOrigin.current;
    if (!origin || !completion.suggestion || !latest.current.completionEnabled || latest.current.disabled || origin.scope !== latest.current.conversationId || origin.draft !== latest.current.draft || origin.revision !== revision.current) return;
    const suggestion = completion.suggestion;
    invalidate(); latest.current.onDraft(suggestion);
  };

  const answerRunId = completedAnswerRun(options.conversationId, options.run, options.messages);
  useEffect(() => {
    const start = latest.current;
    if (start.run?.conversation_id === start.conversationId && isActive(start.run.status)) observedActive.current.add(`${start.conversationId}:${start.run.id}`);
    if (!answerRunId || !start.settled || start.disabled || !start.followupsEnabled || start.draft.length > 0 || start.hasExtraDraft) return;
    const key = `${start.conversationId}:${answerRunId}`;
    // Reading an old conversation never spends a new model request.
    if (!observedActive.current.has(key) || requestedFollowups.current.has(key)) return;
    let alive = true;
    const controller = new AbortController(); const token = revision.current;
    const current = () => alive && mounted.current && !controller.signal.aborted && followupRequest.current === controller && token === revision.current && latest.current.conversationId === start.conversationId && latest.current.run?.id === answerRunId && latest.current.run?.status === 'succeeded' && latest.current.followupsEnabled && !latest.current.disabled && !latest.current.draft.length && !latest.current.hasExtraDraft;
    // Deferring one microtask avoids spending twice on React StrictMode's
    // setup/cleanup probe. Once requested, draft edits never trigger a rerun.
    queueMicrotask(() => {
      if (!alive || !mounted.current || token !== revision.current) return;
      requestedFollowups.current.add(key); followupRequest.current = controller;
      setFollowups({ busy: true, suggestions: [], notice: '', runId: answerRunId });
      void api<{ suggestions: unknown; router: { mode: string } }>('/models/followups', { method: 'POST', body: JSON.stringify({ conversation_id: start.conversationId, run_id: answerRunId }), signal: controller.signal }).then(result => {
        if (current()) setFollowups({ busy: false, suggestions: followupTexts(result.suggestions), notice: '', runId: answerRunId });
      }).catch(() => {
        if (current()) setFollowups({ busy: false, suggestions: [], notice: '后续建议暂不可用。', runId: answerRunId });
      }).finally(() => { if (followupRequest.current === controller) followupRequest.current = null; });
    });
    return () => { alive = false; controller.abort(); };
  }, [options.conversationId, options.run?.id, options.run?.status, answerRunId, options.settled, options.disabled, options.followupsEnabled, options.draft, options.hasExtraDraft]);

  const chooseFollowup = (text: string) => {
    if (latest.current.disabled || latest.current.draft.length || latest.current.hasExtraDraft || latest.current.run?.id !== followups.runId || latest.current.run?.status !== 'succeeded' || !followups.suggestions.includes(text)) return;
    invalidate(); latest.current.onDraft(text);
  };
  const visibleFollowups = options.followupsEnabled && !options.disabled && !options.draft.length && !options.hasExtraDraft && options.run?.id === followups.runId && answerRunId === followups.runId ? followups : emptyFollowups;
  const canPreviewInline = options.completionEnabled && !options.disabled && options.settled && !options.hasExtraDraft
    && (!options.run || !isActive(options.run.status)) && !completion.busy && !completion.suggestion && !previewDismissed;
  let inlineSuggestion = '';
  if (canPreviewInline && !options.draft) {
    inlineSuggestion = visibleFollowups.suggestions[0] || starterQuestion(options.assistantName);
  } else if (canPreviewInline && options.draft.length === 1) {
    inlineSuggestion = shortPrompts[options.draft] || '';
  }
  const acceptSuggestion = () => {
    if (!inlineSuggestion || latest.current.draft !== options.draft || latest.current.conversationId !== options.conversationId) return;
    invalidate();
    latest.current.onDraft(inlineSuggestion);
  };
  const dismiss = () => { invalidate(); setPreviewDismissed(true); };
  return { completion, inlineSuggestion, acceptSuggestion, dismiss, followups: visibleFollowups, complete, accept, invalidate, chooseFollowup, version: () => revision.current };
}

export function ComposerCompletion({ value, onAccept, onDismiss }: { value: Completion; onAccept: () => void; onDismiss: () => void }) {
  if (!value.busy && !value.suggestion && !value.notice) return null;
  return <div className={`composer-completion${value.suggestion ? ' has-preview' : ''}`} role="status" aria-live="polite">
    {value.busy ? <><LoaderCircle className="spin" size={14}/><span>正在补全…</span></> : value.suggestion ? <><div className="composer-completion-copy"><span><Sparkles size={14}/>补全建议</span><p>{value.suggestion}</p></div><button type="button" className="composer-accept" onClick={onAccept}><Check size={14}/>采用<kbd>Tab</kbd></button></> : <span>{value.notice}</span>}
    <button key="completion-dismiss" type="button" className="composer-assist-dismiss" tabIndex={value.busy ? -1 : 0} title={value.busy ? '取消补全（Esc）' : '关闭补全建议'} onClick={onDismiss} aria-label={value.busy ? '取消补全' : '关闭补全建议'}><X size={14}/></button>
  </div>;
}

export function ComposerFollowups({ value, onChoose }: { value: Followups; onChoose: (text: string) => void }) {
  if (!value.busy && !value.suggestions.length && !value.notice) return null;
  if (value.busy || value.notice) return <div className="composer-followups composer-followups-state" role="status">{value.busy ? <span className="composer-followups-pending"><LoaderCircle size={13} className="spin"/>正在整理后续问题…</span> : <span className="composer-followups-notice">{value.notice}</span>}</div>;
  return <details className="composer-followups" aria-label="下一步问题建议">
    <summary><span>继续了解</span><span>{value.suggestions.length} 个问题<ChevronDown size={14}/></span></summary>
    <div>{value.suggestions.map(text => <button key={text} type="button" onClick={() => onChoose(text)}>{text}<ArrowUpLeft size={13}/></button>)}</div>
  </details>;
}
