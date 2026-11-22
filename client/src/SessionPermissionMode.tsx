import { useState } from 'react';
import { ShieldCheck } from 'lucide-react';
import { errorText, patch } from './api';
import { Button, ErrorNotice, Modal } from './components';
import type { Conversation, PermissionMode, StreamEvent } from './types';
import './session-permission-mode.css';

const labels: Record<PermissionMode, string> = { manual: '手动审批', auto: '自动审批', full: '完全权限' };
const descriptions: Record<PermissionMode, string> = {
  manual: '需要确认的操作会暂停等待审批。',
  auto: '管理员核验过的只读工具可自动通过。',
  full: '本会话可直接写入工作区和生成文件，外部操作遵循现有审批策略。',
};

export default function SessionPermissionMode({ conversation, disabled }: { conversation: Conversation; disabled: boolean }) {
  const [saved, setSaved] = useState<PermissionMode | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  const options = conversation.permission_modes ?? [];
  const mode = saved ?? conversation.permission_mode ?? 'manual';
  if (options.length < 2) return null;

  const save = async (next: PermissionMode) => {
    setPending(true);
    setError('');
    try {
      const updated = await patch<Conversation>(`/conversations/${conversation.id}`, { permission_mode: next });
      setSaved(updated.permission_mode ?? next);
      setConfirming(false);
    } catch (cause) {
      setError(errorText(cause));
    } finally {
      setPending(false);
    }
  };

  return <div className="session-permission-mode">
    <label title={descriptions[mode]}>
      <ShieldCheck size={14} aria-hidden="true"/>
      <select aria-label="会话权限" value={mode} disabled={disabled || pending} onChange={event => {
        const next = event.target.value as PermissionMode;
        if (next === 'full') { setError(''); setConfirming(true); }
        else void save(next);
      }}>
        {options.map(value => <option key={value} value={value}>{labels[value]}</option>)}
      </select>
    </label>
    {!confirming && <ErrorNotice message={error} onDismiss={() => setError('')}/>}
    {confirming && <Modal title="开启完全权限" onClose={() => { if (!pending) setConfirming(false); }}>
      <div className="modal-body"><p>{descriptions.full}</p><ErrorNotice message={error}/></div>
      <footer className="modal-footer"><Button type="button" variant="secondary" disabled={pending} onClick={() => setConfirming(false)}>取消</Button><Button type="button" busy={pending} disabled={disabled} onClick={() => void save('full')}>开启完全权限</Button></footer>
    </Modal>}
  </div>;
}

export function MCPAvailabilityNotice({ events }: { events: StreamEvent[] }) {
  const connections = [...new Map(events.filter(event => event.type === 'mcp.unavailable')
    .map(event => [String(event.data.connection_id), event.data])).values()];
  if (!connections.length) return null;
  return <div className="mcp-availability-notice" role="status">
    {connections.map(connection => <p key={String(connection.connection_id)}><strong>{String(connection.name || '业务连接')}暂不可用</strong> · {String(connection.reason || '请查看连接状态')}</p>)}
  </div>;
}
