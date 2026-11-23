import type { StreamEvent } from './types';

/** Replays start at zero when reopening a run; only deltas after a cursor are new. */
export function appendEvent(events: StreamEvent[], next: StreamEvent): StreamEvent[] {
  if (!Number.isInteger(next.id) || next.id < 1) return events;
  if (events.some(event => event.id === next.id && event.run_id === next.run_id)) return events;
  return [...events, next].sort((a, b) => a.id - b.id);
}

export function streamText(events: StreamEvent[]): string {
  const final = [...events].reverse().find(event => event.type === 'message.updated' && typeof event.data.text === 'string');
  if (final) return final.data.text as string;
  return events.filter(event => event.type === 'message.delta').map(event => typeof event.data.text === 'string' ? event.data.text : '').join('');
}

export function pendingApprovalIds(events: StreamEvent[]): Set<string> {
  const pending = new Set<string>();
  for (const event of events) {
    if (event.type === 'approval.required' && typeof event.data.id === 'string') pending.add(event.data.id);
    if (event.type === 'subagent.approval.required' && typeof event.data.approval_id === 'string') pending.add(event.data.approval_id);
    if (['approval.resolved', 'approval.model_decided', 'subagent.approval.denied', 'subagent.approval.resolved'].includes(event.type) && typeof event.data.approval_id === 'string') pending.delete(event.data.approval_id);
    if (event.type === 'run.completed') pending.clear();
  }
  return pending;
}

export const eventTypes = ['run.started', 'run.stage', 'timing.updated', 'message.updated', 'message.delta', 'steer.applied', 'steer.updated', 'tool.started', 'tool.dispatched', 'tool.completed', 'tool.discarded', 'mcp.unavailable', 'approval.required', 'approval.resolved', 'approval.model_decided', 'approval.session_decided', 'approval.dual_vote', 'memory.pending', 'memory.updated', 'memory.aborted', 'subagent.plan.created', 'subagent.started', 'subagent.progress', 'subagent.completed', 'subagent.failed', 'subagent.cancelled', 'subagent.plan.completed', 'subagent.approval.required', 'subagent.approval.denied', 'subagent.approval.resolved', 'artifact.created', 'usage.updated', 'context.compaction_started', 'context.compacted', 'context.compaction_result', 'run.completed'];
