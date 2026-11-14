import { describe, expect, it } from 'vitest';
import { retryMessagePayload } from './ChatPage';

describe('retryMessagePayload', () => {
  it('keeps the original text, attachment ids, and selected skill ids without mutating the message', () => {
    const message = {
      text: '列出逾期采购订单',
      file_ids: ['file-1'],
      skills: [{ id: 'skill-1', name: '采购分析', revision: 3 }],
    };
    const payload = retryMessagePayload(message);
    expect(payload).toEqual({ text: '列出逾期采购订单', fileIds: ['file-1'], skillIds: ['skill-1'] });
    expect(message.file_ids).toEqual(['file-1']);
    expect(message.skills).toHaveLength(1);
  });

  it('uses the same fallback text rules as send when the original text is empty', () => {
    expect(retryMessagePayload({ text: '  ', file_ids: ['file-1'], skills: [] }).text).toBe('请阅读我上传的附件。');
    expect(retryMessagePayload({ text: '  ', file_ids: [], skills: [{ id: 'skill-1', name: '分析', revision: 1 }] }).text).toBe('请执行所选 Skill。');
  });
});
