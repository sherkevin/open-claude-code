import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { RunMetrics } from './ChatPage';
import type { Run } from './types';

const run: Run = {id:'timing',conversation_id:'conversation',status:'succeeded',assistant_message_id:'answer',error:null,usage:null,approvals:[],created_at:'2026-11-14T10:00:00Z',completed_at:'2026-11-14T10:00:01Z'};
describe('honest timing labels', () => {
  it('labels server event boundaries without claiming browser or business result measurements', () => {
    const html = renderToStaticMarkup(<RunMetrics run={{...run,timings:{first_server_progress_ms:120,first_tool_attempt_ms:240,first_tool_success_ms:800}}}/>);
    expect(html).toContain('服务端首个进度');
    expect(html).toContain('首个工具调用尝试');
    expect(html).toContain('首个工具成功返回');
    expect(html).toContain('0.12 秒');
    expect(html).toContain('0.80 秒');
    expect(html).not.toContain('首个结构结果');
    expect(html).not.toContain('首个有效结果');
  });
  it('does not relabel the old ambiguous metrics as measured boundaries', () => {
    // Older traces remain downloadable; their ambiguous keys are not UI metrics.
    const legacy = JSON.parse('{"first_feedback_ms":11,"first_structure_ms":22,"first_valid_result_ms":33}');
    const html = renderToStaticMarkup(<RunMetrics run={{...run,timings:legacy}}/>);
    expect(html).not.toContain('服务端首个进度');
    expect(html).not.toContain('首个工具调用尝试');
    expect(html).not.toContain('首个工具成功返回');
    expect(html).not.toContain('0.01 秒');
  });
});
