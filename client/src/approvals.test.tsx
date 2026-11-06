import { describe, expect, it } from 'vitest';
import { approvalProgress } from './AdminPages';
import type { Approval } from './types';

const base: Approval = { id: 'a', run_id: 'r', tool_name: 'mcp__danger__delete', arguments: {}, status: 'pending', created_at: '', expires_at: '' };

describe('dual approval progress', () => {
  it('requires two distinct approvals by default', () => {
    expect(approvalProgress(base)).toEqual({ votes: 0, required: 2 });
    expect(approvalProgress({ ...base, approver_ids: ['admin-a'], required_approvals: 2 })).toEqual({ votes: 1, required: 2 });
  });
});
