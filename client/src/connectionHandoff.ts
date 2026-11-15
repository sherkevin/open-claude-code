import { api } from './api';
import type { Assistant, Connection } from './types';

export interface ConnectionSetupContext { id: string; name: string }

// Navigation carries identity only; credentials and discovery snapshots stay with the connection.
export const connectionSetupContext = ({ id, name }: Connection): ConnectionSetupContext => ({ id, name });

export function focusConnectionFirst(connections: Connection[], focusConnectionId?: string): Connection[] {
  if (!focusConnectionId) return connections;
  return [
    ...connections.filter(connection => connection.id === focusConnectionId),
    ...connections.filter(connection => connection.id !== focusConnectionId),
  ];
}

export function connectionHandoffReadiness(connectionId: string, connection: Connection | undefined, assistant: Assistant | undefined) {
  if (!connection || connection.id !== connectionId) return { ready: false, message: '当前账号无法读取此连接，请返回连接页核对。' };
  if (!connection.enabled) return { ready: false, message: '此连接已停用，请返回连接页检查。' };
  if (connection.status !== 'connected') return { ready: false, message: '此连接尚未通过当前配置的测试，请返回连接页测试。' };
  if (!connection.tools.length) return { ready: false, message: '连接尚未发现工具，请返回连接页检查。' };
  if (!assistant) return { ready: false, message: '当前账号无法使用此助手，请核对助手的访问权限。' };
  if (!assistant.enabled) return { ready: false, message: '助手已保存但尚未启用，请继续编辑并启用后再试跑。' };
  const binding = assistant.connections.find(item => item.connection_id === connectionId);
  if (!binding) return { ready: false, message: '助手尚未绑定此连接，请继续编辑，勾选连接并选择允许的工具。' };
  if (!connection.tools.some(tool => binding.allowed_tools.includes(tool.name))) return { ready: false, message: '尚未允许此连接中当前已发现的工具，请继续编辑并逐项选择。' };
  return { ready: true, message: '助手已允许使用此连接中的工具。业务结果尚未验证，请在会话中提交具体问题。' };
}

export async function loadConnectionHandoff(connectionId: string, assistantId: string, signal?: AbortSignal) {
  // The ordinary assistant list is intentionally used: manage=true may include assistants this user cannot run.
  const [connections, assistants] = await Promise.all([
    api<Connection[]>('/connections', { signal }),
    api<Assistant[]>('/assistants', { signal }),
  ]);
  const connection = connections.find(item => item.id === connectionId);
  const assistant = assistants.find(item => item.id === assistantId);
  return { connection, assistant, ...connectionHandoffReadiness(connectionId, connection, assistant) };
}
