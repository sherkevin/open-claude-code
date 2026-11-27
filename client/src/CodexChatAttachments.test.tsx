import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import CodexChatAttachments from './CodexChatAttachments';
import { fileURL } from './api';
import type { StoredFile } from './types';

const stored: StoredFile = {
  id: 'uploaded-order', name: '导出说明.md', mime_type: 'text/markdown', size: 97, created_at: '2026-11-28',
};

describe('ordinary chat draft attachments in the engineering dialog', () => {
  it('shows the uploaded originals and explains the text-only engineering request', () => {
    const second = { ...stored, id: 'uploaded-invoice', name: '预览说明.md' };
    const openPreview = vi.fn();
    const html = renderToStaticMarkup(<CodexChatAttachments files={[stored, second]} onPreview={openPreview}/>);
    expect(html).toContain('附件仅供你核对原件');
    expect(html).toContain('工程任务只发送你输入的文本');
    for (const file of [stored, second]) {
      expect(html).toContain(`title="${file.name}"`);
      expect(html).toContain(`href="${fileURL(file.id)}"`);
    }
    expect(html).not.toContain('移除');
    expect(openPreview).not.toHaveBeenCalled();
  });

  it('omits the entire section when the ordinary chat has no uploaded draft files', () => {
    expect(renderToStaticMarkup(<CodexChatAttachments files={[]} onPreview={vi.fn()}/>)).toBe('');
  });
});
