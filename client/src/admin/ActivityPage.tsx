import { useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { api, errorText, timeText } from '../api';
import { Button, ErrorNotice, Field, Loader, PageHeader } from '../components';
import type { User } from '../types';

const operations: Record<string, string> = {
  chat: '业务问答', compact: '上下文压缩', routing: '自动选模', completion: '输入补全',
  followups: '后续问题', knowledge_vision: '知识图片解析', 'tool.started': '工具发起',
  'tool.completed': '工具返回', 'tool.discarded': '工具结果丢弃', prompt_review: '消息审核', prompt_policy: '策略变更',
};
const statuses: Record<string, string> = {
  succeeded: '成功', failed: '失败', blocked: '已拦截', queued: '排队中', running: '执行中',
  started: '已发起', cancelling: '正在取消', cancelled: '已取消', interrupted: '中断',
  not_sent: '未发送', discarded: '已丢弃', unknown: '结果未知', waiting_approval: '待审批',
};

type Activity = { id: string; created_at: string; display_name: string; username: string; actor_name: string | null;
  operation: string; status: string; code: string | null; tool_name: string | null;
  model_label?: string; input_tokens?: number | null; output_tokens?: number | null };
type ActivityData = {items: Activity[]; next_cursor: string | null};
const tokenDisplay = (value: number | null | undefined) => value === undefined ? '—' : value === null ? '未报告' : value.toLocaleString('zh-CN');

export default function ActivityPage({users}: {users: User[]}) {
  const [data, setData] = useState<ActivityData | null>(null);
  const [userId, setUserId] = useState('');
  const [cursor, setCursor] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    const params = new URLSearchParams({limit: '50'});
    if (userId) params.set('user_id', userId);
    if (cursor) params.set('before', cursor);
    setLoading(true);
    setError('');
    void api<ActivityData>(`/admin/activity?${params}`, {signal: controller.signal}).then(setData)
      .catch(cause => {if (!controller.signal.aborted) setError(errorText(cause));})
      .finally(() => {if (!controller.signal.aborted) setLoading(false);});
    return () => controller.abort();
  }, [cursor, loaded, userId]);
  const refresh = () => {setCursor(null); setLoaded(value => value + 1);};
  return <div className="page ops-activity">
    <PageHeader eyebrow="运营数据" title="操作日志" description="按时间查看任务、模型、工具和审核操作。"
      actions={<Button variant="secondary" disabled={loading} onClick={refresh}><RefreshCw size={16}/>刷新</Button>}/>
    <Field label="用户"><select value={userId} onChange={event => {setUserId(event.target.value); setCursor(null);}}>
      <option value="">所有用户</option>{users.map(user => <option key={user.id} value={user.id}>{user.display_name}（{user.username}）</option>)}
    </select></Field>
    <ErrorNotice message={error}/>
    {loading ? <Loader text="正在读取操作日志…"/> : !error && data && <>
      {!data.items.length ? <p className="ops-empty">当前范围没有操作记录。</p> : <div className="table-container">
        <table><thead><tr><th>时间</th><th>用户</th><th>操作</th><th>结果</th><th>模型</th><th>输入 Token</th><th>输出 Token</th></tr></thead>
          <tbody>{data.items.map(item => <tr key={item.id}>
            <td><time dateTime={item.created_at}>{timeText(item.created_at)}</time></td><td>{item.display_name}<small className="ops-row-note">{item.username}</small>{item.actor_name && <small className="ops-row-note">操作人 {item.actor_name}</small>}</td>
            <td>{operations[item.operation] || '其他已记录操作'}{item.tool_name && <small className="ops-row-note">{item.tool_name}</small>}</td>
            <td>{statuses[item.status] || '状态未报告'}{item.code && <small className="ops-row-note">{item.code}</small>}</td>
            <td>{item.model_label || '—'}</td><td>{tokenDisplay(item.input_tokens)}</td><td>{tokenDisplay(item.output_tokens)}</td>
          </tr>)}</tbody>
        </table>
      </div>}
      <div className="ops-paging">
        <Button variant="secondary" disabled={!cursor} onClick={refresh}>返回最新</Button>
        <Button variant="secondary" disabled={!data.next_cursor} onClick={() => setCursor(data.next_cursor)}>查看更早记录</Button>
      </div>
    </>}
  </div>;
}
