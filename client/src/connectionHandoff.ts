import { api } from './api';
import type { Assistant, Connection } from './types';

export interface ConnectionSetupContext { id: string; name: string }

// Navigation carries identity only; credentials and discovery snapshots stay with the connection.
export const connectionSetupContext = ({ id, name }: Connection): ConnectionSetupContext => ({ id, name });

// Both entry points must retain the target for the submit-time permission check.
export const connectionTrialSession = (assistant: Assistant, connection: Readonly<ConnectionSetupContext>) => ({
  assistant, connectionSetup: { id: connection.id, name: connection.name },
});

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
  if (!assistant.enabled) return { ready: false, message: '助手已保存但尚未启用，请启用后再验证业务结果。' };
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

// Reuse the same server-authoritative grants checked before creating a
// guided conversation. The ordinary list excludes assistants the user cannot run.
export async function loadConnectionTrialOptions(connectionId: string, signal?: AbortSignal) {
  const [connections, assistants] = await Promise.all([
    api<Connection[]>('/connections', { signal }),
    api<Assistant[]>('/assistants', { signal }),
  ]);
  const connection = connections.find(item => item.id === connectionId);
  return { connection, assistants: assistants.filter(assistant => connectionHandoffReadiness(connectionId, connection, assistant).ready) };
}

export function connectionTrialQuestion(connection: Readonly<ConnectionSetupContext>, question: string) {
  return '请使用「' + connection.name + '」中已授权的只读业务工具，回答：' + question.trim()
    + '\n请按实际工具返回结果作答，明细或统计优先用表格；如缺少对应数据集、字段、权限或工具执行失败，明确说明，不使用演示数据，不编造、不修改业务数据。';
}
