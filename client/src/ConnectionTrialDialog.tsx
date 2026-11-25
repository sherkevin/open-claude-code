import { useEffect, useRef, useState } from 'react';
import { ArrowRight, RefreshCw } from 'lucide-react';
import { errorText } from './api';
import { Button, ErrorNotice, Field, Loader, Modal } from './components';
import { connectionSetupContext, connectionTrialQuestion, loadConnectionHandoff, loadConnectionTrialOptions, type ConnectionSetupContext } from './connectionHandoff';
import type { Assistant } from './types';

export default function ConnectionTrialDialog({ target, onClose, onStart, onConfigure }: {
  target: Readonly<ConnectionSetupContext>;
  onClose: () => void;
  onStart: (assistant: Assistant, target: Readonly<ConnectionSetupContext>, question: string) => void;
  onConfigure?: (target: Readonly<ConnectionSetupContext>) => void;
}) {
  const [options, setOptions] = useState<Awaited<ReturnType<typeof loadConnectionTrialOptions>> | null>(null);
  const [selected, setSelected] = useState('');
  const [question, setQuestion] = useState('');
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [retry, setRetry] = useState(0);
  const pending = useRef(false);
  const request = useRef<AbortController | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError(''); setOptions(null); setSelected('');
    loadConnectionTrialOptions(target.id, controller.signal).then(result => {
      if (controller.signal.aborted) return;
      setOptions(result);
      if (result.assistants.length === 1) setSelected(result.assistants[0].id);
    }).catch(cause => {
      if (!controller.signal.aborted) setError(errorText(cause));
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false);
    });
    return () => { controller.abort(); request.current?.abort(); };
  }, [target.id, retry]);
  const connection = options?.connection;
  const discovered = Boolean(connection?.enabled && connection.status === 'connected' && connection.tools.length);
  const ready = discovered && options?.assistants.some(a => a.id === selected) && Boolean(question.trim());
  const close = () => { if (!pending.current) onClose(); };
  return <Modal title="验证业务结果" description="工具发现不代表业务执行成功。使用实际业务问题完成验证。" onClose={close}>
    <form onSubmit={async event => {
      event.preventDefault();
      if (!ready || pending.current) return;
      const controller = new AbortController(); request.current = controller;
      pending.current = true; setBusy(true); setError('');
      try {
        const fresh = await loadConnectionHandoff(target.id, selected, controller.signal);
        if (controller.signal.aborted) return;
        if (!fresh.ready || !fresh.assistant || !fresh.connection) throw new Error(fresh.message);
        const context = connectionSetupContext(fresh.connection);
        onStart(fresh.assistant, context, connectionTrialQuestion(context, question));
      } catch (cause) {
        if (!controller.signal.aborted) setError(errorText(cause));
      } finally {
        pending.current = false;
        if (!controller.signal.aborted) setBusy(false);
      }
    }}>
      <div className="modal-body">
        <p><strong>验证连接：{target.name}</strong></p>
        <ErrorNotice message={error}/>
        {loading ? <Loader text="正在检查连接及助手权限…"/> : options ? !discovered ? <p>当前连接未通过工具发现，请返回 MCP 页面测试连接。</p> : !options.assistants.length ? <p>尚无可使用此连接的助手，请先选择助手并明确授权工具。</p> : <>
          <Field label="使用助手" required><select value={selected} onChange={event => setSelected(event.target.value)} disabled={busy} required><option value="" disabled>请选择助手</option>{options.assistants.map(assistant => <option key={assistant.id} value={assistant.id}>{assistant.name}</option>)}</select></Field>
          <Field label="实际业务问题" hint="例如：最近三笔销售订单是什么？最近一个月的成交金额按周汇总。请结合实际开放的业务数据选择问题。" required><textarea rows={4} maxLength={2000} value={question} onChange={event => setQuestion(event.target.value)} disabled={busy} placeholder="输入希望用真实业务数据回答的问题" required/></Field>
          <p className="form-note">打开会话后可编辑并发送问题。保留原有助手权限和工具审批，不会自动执行工具。验收时确认使用目标连接的真实查询结果。</p>
        </> : <Button variant="secondary" onClick={() => setRetry(value => value + 1)}><RefreshCw size={15}/>重新核对</Button>}
      </div>
      <footer className="modal-footer"><Button variant="secondary" disabled={busy} onClick={close}>取消</Button>
        {discovered && onConfigure && <Button variant="secondary" disabled={busy} onClick={() => { if (connection && !pending.current) onConfigure(connectionSetupContext(connection)); }}>配置助手</Button>}
        {discovered && !!options?.assistants.length && <Button type="submit" busy={busy} disabled={!ready}>开始业务验证<ArrowRight size={16}/></Button>}
      </footer>
    </form>
  </Modal>;
}
