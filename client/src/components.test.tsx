import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { Button, ErrorNotice, FileChip, Modal } from './components';
import type { StoredFile } from './types';

const file: StoredFile = { id: 'attachment-1', name: '资料.txt', mime_type: 'text/plain', size: 18, created_at: '2026-11-05T00:00:00Z' };
const buttonTags = (html: string) => html.match(/<button\b[^>]*>/g) || [];

describe('non-submit controls inside forms', () => {
  it('attachment preview and removal never implicitly submit the chat form', () => {
    const html = renderToStaticMarkup(<form><FileChip file={file} onPreview={() => {}} onRemove={() => {}}/></form>);
    const buttons = buttonTags(html);
    expect(buttons).toHaveLength(2);
    expect(buttons.every(button => button.includes('type="button"'))).toBe(true);
  });
  it('shared controls default to non-submit while an explicit submit is preserved', () => {
    const html = renderToStaticMarkup(<form><Button>取消</Button><Button type="submit">发送</Button><ErrorNotice message="请重试" onDismiss={() => {}}/></form>);
    expect(buttonTags(html).map(tag => tag.match(/type="([^"]+)"/)?.[1])).toEqual(['button', 'submit', 'button']);
  });
  it('a modal close control cannot accidentally submit an ancestor form', () => {
    const html = renderToStaticMarkup(<form><Modal title="预览" onClose={() => {}}>内容</Modal></form>);
    expect(buttonTags(html)).toHaveLength(1);
    expect(buttonTags(html)[0]).toContain('type="button"');
  });
});
