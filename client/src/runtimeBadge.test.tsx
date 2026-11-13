import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import RuntimeBadge from './RuntimeBadge';

describe('conversation executor identity', () => {
  it('labels a stored Harness conversation, not its answering model', () => {
    const html = renderToStaticMarkup(<RuntimeBadge runtime="harness" />);
    expect(html).toContain('本会话执行框架：DeepSeek Harness');
    expect(html).toContain('执行框架与回答模型是不同配置');
    expect(html).not.toContain('Claude Agent SDK');
  });

  it('keeps legacy conversation identity explicit', () => {
    const html = renderToStaticMarkup(<RuntimeBadge runtime="claude" />);
    expect(html).toContain('本会话执行框架：Claude Agent SDK');
    expect(html).toContain('新会话默认值不会迁移本会话');
    expect(html).not.toContain('DeepSeek Harness');
  });

  it('does not invent an executor for responses from older servers', () => {
    const html = renderToStaticMarkup(<RuntimeBadge runtime={undefined} />);
    expect(html).toContain('执行框架待确认');
    expect(html).not.toContain('DeepSeek Harness');
    expect(html).not.toContain('Claude Agent SDK');
  });
});
