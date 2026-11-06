import { RefreshCw } from 'lucide-react';
import { Button } from './components';

export default function ModelRecovery({ currentAvailable, busy, pending, onReconnect }: { currentAvailable: boolean; busy: boolean; pending: boolean; onReconnect: () => void }) {
  return <div className="model-recovery">
    {currentAvailable ? <><p>模型服务配置已更新。重新连接后，请自行重新发送消息；已有任务不会自动重放。</p><Button variant="secondary" disabled={busy} busy={pending} onClick={onReconnect}><RefreshCw size={14}/>重新连接当前模型</Button></> : <p>当前历史模型不在可用列表中，请选择可用模型；标记“需新对话”的选项请在新对话中使用。当前对话和记忆保留。</p>}
  </div>;
}
