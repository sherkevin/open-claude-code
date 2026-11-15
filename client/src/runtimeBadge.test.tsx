import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import RuntimeBadge from './RuntimeBadge';

describe('conversation executor identity', () => {
  it('labels a stored Harness conversation, not its answering model', () => {
    const html = renderToStaticMarkup(<RuntimeBadge runtime="harness" />);
    expect(html).toContain('本会话执行引擎：标准引擎');
    expect(html).toContain('回答使用你选择的模型');
    expect(html).not.toMatch(/DeepSeek|Harness|Claude|SDK/i);
  });

  it('keeps legacy conversation identity explicit', () => {
    const html = renderToStaticMarkup(<RuntimeBadge runtime="claude" />);
    expect(html).toContain('本会话执行引擎：兼容引擎');
    expect(html).toContain('保留原有历史');
    expect(html).not.toMatch(/DeepSeek|Harness|Claude|SDK/i);
  });

  it('does not invent an executor for responses from older servers', () => {
    const html = renderToStaticMarkup(<RuntimeBadge runtime={undefined} />);
    expect(html).toContain('执行引擎待确认');
    expect(html).not.toMatch(/DeepSeek|Harness|Claude|SDK/i);
  });
});
