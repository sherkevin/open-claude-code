import { describe, expect, it } from 'vitest';
import { isFeishuEmbed } from './feishu';

describe('Feishu H5 entry detection', () => {
  it('recognizes the configured app path and trailing slash', () => {
    expect(isFeishuEmbed({ pathname: '/feishu', search: '' })).toBe(true);
    expect(isFeishuEmbed({ pathname: '/feishu/', search: '' })).toBe(true);
  });

  it('keeps normal browser routes out of the Feishu login flow', () => {
    expect(isFeishuEmbed({ pathname: '/', search: '' })).toBe(false);
    expect(isFeishuEmbed({ pathname: '/workspace', search: '?embed=other' })).toBe(false);
    expect(isFeishuEmbed({ pathname: '/workspace', search: '?embed=feishu' })).toBe(true);
  });
});
