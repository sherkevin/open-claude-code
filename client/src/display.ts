import type { CompactionStatus } from './types';

/** Display only: preserve raw tool names in API requests, stored events and traces. */
export function toolLabel(name: string): string {
  const memoryTools:Record<string,string> = { mcp__workspace__memory_list:'列出当前会话记忆', mcp__workspace__memory_read:'读取当前会话记忆', mcp__workspace__memory_write:'申请保存会话记忆' };
  if (name === 'mcp__workspace__schedule_create') return '创建定时智能体任务';
  if (memoryTools[name]) return memoryTools[name];
  return name.replace(/^mcp__c[0-9a-f]{32}__(.+)$/i, '$1');
}


export function compactionStatus(value: unknown): CompactionStatus {
  return typeof value === 'string' && ['started', 'completed', 'not_needed', 'failed', 'cancelled', 'interrupted'].includes(value) ? value as CompactionStatus : 'unknown';
}

export function compactionStatusLabel(value: unknown, followup?: unknown): string {
  const labels: Record<CompactionStatus, string> = {
    started: '正在执行原生压缩', completed: '已发生原生上下文压缩',
    not_needed: '未发生压缩（可能历史较少）', failed: '原生压缩失败',
    cancelled: '原生压缩已停止', interrupted: '原生压缩已中断', unknown: '未提供原生压缩结果',
  };
  const status = compactionStatus(value);
  if (status === 'completed') {
    if (followup === 'failed') return '已发生原生上下文压缩，后续步骤失败';
    if (followup === 'cancelled') return '已发生原生上下文压缩，后续步骤已停止';
    if (followup === 'interrupted') return '已发生原生上下文压缩，后续步骤已中断';
  }
  return labels[status];
}
