import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { OriginalCodexReceipt, type CodexReceipt } from './ConversationCodex';
import { describe, expect, it } from 'vitest';
import { changeCodexDraft, type DraftAttempt } from './codexDraft';

const text = '  原样保留\n\n第二行  ';
const initial = { text, revision: 'typed-A', attempts: [] };
const attempt: DraftAttempt = { kind: 'turn', id: 'old-request', text };
const submit = { kind: 'submit' as const, base: 'typed-A', revision: 'claimed', attempt };

describe('工程草稿与已尝试提交的原文', () => {
  it('keeps exact text apart from the input when an explicit submission is claimed', () => {
    const sent = changeCodexDraft(initial, submit);
    expect(sent).toEqual({ text: '', revision: 'claimed', attempts: [attempt] });
    expect(changeCodexDraft(initial, { ...submit, attempt: { ...attempt, text: text.trim() } })).toBe(initial);
  });

  it('applies an edit or clear only to its observed version, including repeated identical text', () => {
    const newer = changeCodexDraft(initial, { kind: 'edit', base: 'typed-A', revision: 'typed-B', text });
    expect(changeCodexDraft(newer, { kind: 'edit', base: 'typed-A', revision: 'stale', text: '' })).toBe(newer);
    expect(changeCodexDraft(newer, submit)).toBe(newer);
    const cleared = changeCodexDraft(newer, { kind: 'edit', base: 'typed-B', revision: 'cleared', text: '' });
    expect(cleared.text).toBe('');
    expect(changeCodexDraft(cleared, { kind: 'edit', base: 'typed-B', revision: 'stale', text })).toBe(cleared);
  });

  it('an old acknowledgement can clear only its snapshot, not a new draft or an unrelated steer', () => {
    const original = changeCodexDraft(initial, submit);
    const next = changeCodexDraft(original, { kind: 'edit', base: 'claimed', revision: 'next', text: '补充' });
    const steered = changeCodexDraft(next, { kind: 'submit', base: 'next', revision: 'steered', attempt: { kind: 'steer', id: 'steer-id', turn: 'target', text: '补充' } });
    const edited = changeCodexDraft(steered, { kind: 'edit', base: 'steered', revision: 'edited', text: '  新草稿 ' });
    const ack = changeCodexDraft(edited, { kind: 'settle', id: 'old-request' });
    expect(ack).toEqual({ text: '  新草稿 ', revision: 'edited', attempts: [steered.attempts[1]] });
    expect(changeCodexDraft(ack, { kind: 'settle', id: 'old-request' })).toBe(ack);
  });

  it('does not start a second ordinary submission while the first explicit submission remains unresolved', () => {
    const claimed = changeCodexDraft(initial, submit);
    const next = changeCodexDraft(claimed, { kind: 'edit', base: 'claimed', revision: 'next', text });
    expect(changeCodexDraft(next, { ...submit, base: 'next', attempt: { ...attempt, id: 'new-id' } })).toBe(next);
  });
  it('requires the old text only when a missing receipt has no saved original', () => {
    const receipt: CodexReceipt = { state: 'not_found', request_id: 'unknown', turn: null };
    const current = renderToStaticMarkup(createElement(OriginalCodexReceipt, { receipt, hasRetrySnapshot: true }));
    expect(current).toContain('已从本浏览器找回提交原文');
    expect(current).not.toContain('输入原内容');
    expect(renderToStaticMarkup(createElement(OriginalCodexReceipt, { receipt }))).toContain('输入原内容');
  });
});
