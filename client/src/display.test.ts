import { describe, expect, it } from 'vitest';
import { toolLabel } from './display';

describe('tool labels', () => {
  it('hides only the internal connection prefix and preserves the full tool name', () => {
    const original = 'mcp__c0123456789abcdef0123456789abcdef__knowledge__search';
    expect(toolLabel(original)).toBe('knowledge__search');
    expect(original).toBe('mcp__c0123456789abcdef0123456789abcdef__knowledge__search');
  });
  it('keeps native and unrelated MCP names unchanged', () => {
    for (const name of ['Read', 'search_knowledge', 'mcp__customer__search', 'mcp__c0123__search']) expect(toolLabel(name)).toBe(name);
  });
});
