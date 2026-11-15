import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowRight, BookOpen, Globe, Sparkles, Bot, Network, Pencil, Plus, RefreshCw, ShieldCheck, Trash2, Users } from 'lucide-react';
import { api, errorText, remove } from './api';
import type { Assistant } from './types';
import { canEdit, ScopeBadge } from './resources';
import { Button, EmptyState, ErrorNotice, Loader, Modal, PageHeader, SuccessNotice } from './components';
import AssistantConfiguration from './AssistantConfiguration';
import useAssistantConfiguration from './useAssistantConfiguration';
import type { AssistantDraftNavigation } from './AssistantDetailPage';
import { loadConnectionHandoff, type ConnectionSetupContext } from './connectionHandoff';
import AiIdentity from './AiIdentity';

export default function AssistantsPage({ onChanged, onOpen, admin, onDirtyChange, onSavingChange, requestNavigation, draftEpoch, connectionSetup, onReturnToConnection, onStartAssistant }: {
  onChanged: () => void;
  onOpen: (assistant: Assistant) => void;
  admin: boolean;
  connectionSetup?: ConnectionSetupContext | null;
  onReturnToConnection?: () => void;
  onStartAssistant?: (assistant: Assistant) => void;
} & AssistantDraftNavigation) {
  const [items, setItems] = useState<Assistant[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');
  const [editor, setEditor] = useState<Assistant | 'new' | null>(null);
  const [savedForConnection, setSavedForConnection] = useState<{ connectionId: string; assistant: Assistant } | null>(null);
  const [deleting, setDeleting] = useState<Assistant | null>(null);
  const [deletingBusy, setDeletingBusy] = useState(false);
  const load = useCallback(async () => {
    setError('');
    try { setItems(await api<Assistant[]>('/assistants?manage=true')); }
    catch (e) { setError(errorText(e)); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void load(); }, [load]);
  const openEditor = (assistant: Assistant | 'new') => requestNavigation(() => { setSuccess(''); setEditor(assistant); });
  const saved = savedForConnection?.connectionId === connectionSetup?.id ? savedForConnection?.assistant : undefined;
  return <div className="page">
    <PageHeader eyebrow="业务配置" title="助手设置" description="设置助手的业务规则、数据连接与成员权限。" actions={!connectionSetup && <Button onClick={() => openEditor('new')}><Plus size={17}/>新建助手</Button>}/>
    {connectionSetup && <section className="connection-setup-context" aria-label="连接配置引导">
      <div><h2>为「{connectionSetup.name}」配置助手</h2><p>选择下方有编辑权限的现有助手，或新建助手。进入配置后，请勾选此连接并选择允许的工具。</p></div>
      <div className="connection-setup-actions"><Button onClick={() => openEditor('new')}><Plus size={16}/>新建助手</Button>{onReturnToConnection && <Button variant="secondary" onClick={onReturnToConnection}>返回该连接</Button>}</div>
    </section>}
    <ErrorNotice message={error} onDismiss={() => setError('')}/><SuccessNotice message={success}/>
    {connectionSetup && saved && !editor && <SavedConnectionHandoff key={`${connectionSetup.id}-${saved.id}-${saved.revision}`} setup={connectionSetup} saved={saved} onReturn={onReturnToConnection} onEdit={() => openEditor(saved)} onStart={onStartAssistant}/>}
    {loading ? <Loader/> : !items.length ? <EmptyState icon={<Bot size={32}/>} title="尚未创建助手" description="新建助手，填写业务规则并选择需要的数据连接。" action={<Button onClick={() => openEditor('new')}><Plus size={17}/>新建助手</Button>}/> : <div className="assistant-settings-list">{items.map(a => <article key={a.id} className="assistant-setting-card">
      <AiIdentity size="small"/><div className="assistant-setting-content"><div className="card-title-line"><h2>{a.name}</h2><ScopeBadge scope={a.scope}/><span className={`badge ${a.enabled ? 'green' : ''}`}>{a.enabled ? '已启用' : '已停用'}</span></div><p>{a.description || '暂无描述'}</p><div className="setting-tags"><span><Network size={14}/>{a.connections.length} 个业务连接</span><span><BookOpen size={14}/>{a.knowledge_base_ids?.length || 0} 个知识库</span><span><Sparkles size={14}/>{a.skill_ids?.length || 0} 个 Skills</span>{a.web_search_enabled && <span><Globe size={14}/>网页搜索</span>}<span><Users size={14}/>{a.scope === 'private' ? '仅自己' : a.access.all_members ? '所有成员' : `${a.access.user_ids.length} 位指定成员`}</span><span><ShieldCheck size={14}/>{a.connections.reduce((n, c) => n + c.allowed_tools.length - c.auto_approved_tools.length, 0)} 个工具需确认</span></div></div>
      <div className="setting-actions"><Button variant="ghost" onClick={() => onOpen(a)}>查看助手<ArrowRight size={14}/></Button>{canEdit(a, admin) && <><Button variant="secondary" onClick={() => openEditor(a)}><Pencil size={15}/>{connectionSetup ? '配置此助手' : '编辑'}</Button><button className="icon-button danger-text" aria-label={`删除 ${a.name}`} onClick={() => setDeleting(a)}><Trash2 size={17}/></button></>}</div>
    </article>)}</div>}
    {editor && <AssistantEditor key={editor === 'new' ? 'new' : editor.id} id={editor === 'new' ? null : editor.id} admin={admin} focusConnectionId={connectionSetup?.id} draftEpoch={draftEpoch} onDirtyChange={onDirtyChange} onSavingChange={onSavingChange} requestNavigation={requestNavigation} onClose={() => setEditor(null)} onSaved={assistant => {
      setEditor(null);
      if (connectionSetup) { setSavedForConnection({ connectionId: connectionSetup.id, assistant }); setSuccess(''); }
      else setSuccess('助手设置已保存。');
      void load(); onChanged();
    }}/>}
    {deleting && <Modal title="删除助手" onClose={() => { if (!deletingBusy) setDeleting(null); }}><div className="modal-body"><p>删除「{deleting.name}」后，成员将不能再通过它开始新的工作。已有对话记录仍然保留。</p></div><footer className="modal-footer"><Button variant="secondary" disabled={deletingBusy} onClick={() => setDeleting(null)}>保留助手</Button><Button variant="danger" busy={deletingBusy} onClick={async () => { setDeletingBusy(true); try { await remove(`/assistants/${deleting.id}`); if (savedForConnection?.assistant.id === deleting.id) setSavedForConnection(null); setDeleting(null); await load(); onChanged(); } catch (e) { setError(errorText(e)); setDeleting(null); } finally { setDeletingBusy(false); } }}>确认删除</Button></footer></Modal>}
  </div>;
}

function SavedConnectionHandoff({ setup, saved, onReturn, onEdit, onStart }: {
  setup: ConnectionSetupContext; saved: Assistant; onReturn?: () => void; onEdit: () => void; onStart?: (assistant: Assistant) => void;
}) {
  const [snapshot, setSnapshot] = useState<Awaited<ReturnType<typeof loadConnectionHandoff>> | null>(null);
  const [checking, setChecking] = useState(true);
  const [error, setError] = useState('');
  const request = useRef<AbortController | null>(null);
  const start = useRef(onStart); start.current = onStart;
  const verify = useCallback(async (openSession = false) => {
    request.current?.abort();
    const controller = new AbortController(); request.current = controller;
    setChecking(true); setError(''); setSnapshot(null);
    try {
      const next = await loadConnectionHandoff(setup.id, saved.id, controller.signal);
      if (controller.signal.aborted) return;
      setSnapshot(next);
      if (openSession && next.ready && next.assistant) start.current?.(next.assistant);
    } catch (e) { if (!controller.signal.aborted) setError(`验证条件尚未确认：${errorText(e)}`); }
    finally { if (!controller.signal.aborted) setChecking(false); }
  }, [setup.id, saved.id]);
  useEffect(() => { void verify(); return () => request.current?.abort(); }, [verify]);
  return <section className="connection-setup-context" aria-label="助手保存后的下一步">
    <div><h2>「{saved.name}」已保存</h2><p role="status">{checking ? '正在核对当前连接、助手和工具权限…' : snapshot?.message || '验证条件尚未确认，请返回连接页检查。'}</p>{!onStart && <p>当前账号没有新建会话权限，请联系管理员。</p>}<ErrorNotice message={error}/></div>
    <div className="connection-setup-actions">
      {onReturn && <Button variant="secondary" onClick={onReturn}>返回该连接</Button>}
      <Button variant="secondary" onClick={onEdit}>继续编辑助手</Button>
      {!checking && snapshot?.ready && onStart && <Button onClick={() => void verify(true)}>验证业务结果<ArrowRight size={16}/></Button>}
      {error && <Button variant="ghost" busy={checking} onClick={() => void verify()}><RefreshCw size={15}/>重新核对</Button>}
    </div>
  </section>;
}

function AssistantEditor({ id, admin, onClose, onSaved, onDirtyChange, onSavingChange, requestNavigation, draftEpoch, focusConnectionId }: {
  id: string | null; admin: boolean; onClose: () => void; onSaved: (assistant: Assistant) => void; focusConnectionId?: string;
} & AssistantDraftNavigation) {
  const { snapshot, loading, error, generation, reload } = useAssistantConfiguration(id, admin);
  const close = () => requestNavigation(onClose);
  return <Modal title={id ? '编辑助手' : '新建助手'} description="设置业务规则、工具和资料范围。" wide onClose={close}><div className="modal-body assistant-editor-body">{loading ? <Loader text="正在加载助手配置…"/> : error || !snapshot ? <><ErrorNotice message={error || '无法读取配置。'}/><Button variant="secondary" onClick={reload}><RefreshCw size={16}/>重新加载</Button></> : <AssistantConfiguration key={`${generation}-${draftEpoch}`} value={snapshot.assistant} resources={snapshot.resources} admin={admin} focusConnectionId={focusConnectionId} onDirtyChange={onDirtyChange} onSavingChange={onSavingChange} onSaved={onSaved} onReload={() => requestNavigation(reload)} onCancel={close}/>}</div></Modal>;
}
