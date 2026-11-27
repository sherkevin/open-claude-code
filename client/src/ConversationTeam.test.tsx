import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import ConversationTeam, {
  TeamBoardView, operationLabel, parseWriteScopes, teamTaskDraft, teamTransition,
  type NativeTeamTask, type TeamBoard, type TeamOperation,
} from './ConversationTeam';

const task: NativeTeamTask = {
  id: 'task-0001', revision: 3, subject: '汇总交付清单', description: '合成任务',
  status: 'pending', blockedBy: ['task-0000'], writeScopes: ['server/a'],
  ready: false, writeScopeWarnings: ['范围与另一任务重叠'],
};

describe('native Team task lifecycle UI helpers', () => {
  it('normalizes only explicit task metadata fields', () => {
    expect(parseWriteScopes('server/a\n server/b, server/a')).toEqual(['server/a', 'server/b']);
    expect(teamTaskDraft('  汇总  ', '  只维护任务板  ', ['task-0001', 'task-0001'], ['server/a']))
      .toEqual({ subject: '汇总', description: '只维护任务板',
        blocked_by: ['task-0001'], write_scopes: ['server/a'] });
    expect(() => teamTaskDraft(' ', 'body', [], [])).toThrow();
    expect(() => teamTaskDraft('x'.repeat(201), 'body', [], [])).toThrow();
    expect(() => teamTaskDraft('title', ' ', [], [])).toThrow();
    expect(() => teamTaskDraft('title', 'body', ['bad id'], [])).toThrow();
    expect(() => parseWriteScopes('x'.repeat(301))).toThrow();
  });

  it('uses only the exact CAS revision in a transition', () => {
    expect(teamTransition(task, 'claim')).toEqual({ expected_revision: 3 });
    expect(() => teamTransition({ ...task, revision: 0 }, 'claim')).toThrow();
  });

  it('renders board status, dependency readiness and revisions without execution claims', () => {
    const board: TeamBoard = { enabled: true, tasks: [task] };
    const html = renderToStaticMarkup(<TeamBoardView board={board} />);
    expect(html).toContain('任务板当前已启用');
    expect(html).toContain('汇总交付清单');
    expect(html).toContain('待认领');
    expect(html).toContain('依赖未完成');
    expect(html).toContain('版本 3');
    expect(html).toContain('范围与另一任务重叠');
    expect(html).not.toContain('已完成');
  });

  it('labels uncertain operations distinctly and keeps authorization history reachable while busy', () => {
    const operation: TeamOperation = {
      id: 'op-1', client_request_id: 'request-1', action: 'claim', status: 'uncertain',
      error_code: 'TEAM_TASK_UNCERTAIN', task_id: null, task_revision: null, task_status: null,
      board_enabled: null, acknowledged_at: null, created_at: '2026-11-26T00:00:00+00:00',
      updated_at: '2026-11-26T00:00:00+00:00',
    };
    expect(operationLabel(operation)).toBe('认领任务 · 结果未确认');
    expect(renderToStaticMarkup(<ConversationTeam conversationId="fixture" unavailable={false}/>))
      .toContain('团队任务板');
    expect(renderToStaticMarkup(<ConversationTeam conversationId="fixture" unavailable/>))
      .not.toContain('disabled');
  });
});
