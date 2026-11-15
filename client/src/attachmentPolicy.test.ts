import { describe, expect, it } from 'vitest';
import { CHAT_ATTACHMENT_ACCEPT, chatAttachmentProblem } from './attachmentPolicy';

const MiB = 1024 * 1024;
const file = (name: string, size: number) => ({ name, size });
const check = (files: Array<{ name: string; size: number }>) => chatAttachmentProblem(files, 'harness', 20 * MiB);

describe('composer attachment admission', () => {
  it('advertises exact supported raster extensions rather than image/*', () => {
    expect(CHAT_ATTACHMENT_ACCEPT).not.toContain('image/*');
    expect(CHAT_ATTACHMENT_ACCEPT.split(',')).toEqual(expect.arrayContaining(['.png', '.jpeg', '.gif', '.xlsx', '.yaml']));
    expect(check([file('photo.HEIC', 100)])).toContain('暂不支持');
    expect(check([file('vector.svg', 100)])).toContain('暂不支持');
    expect(check([file('photo.JPEG', 100), file('note.yaml', 100)])).toBeNull();
  });
  it('checks existing plus selected files as one bounded message', () => {
    const existing = Array.from({ length: 9 }, (_, i) => file(`saved-${i}.txt`, 100));
    expect(check([...existing, file('one.txt', 100)])).toBeNull();
    expect(check([...existing, file('one.txt', 100), file('two.txt', 100)])).toContain('10 个');
  });
  it('allows exact binary size boundaries and rejects one excess byte', () => {
    expect(check([file('document.pdf', 10 * MiB), file('image.png', 5 * MiB)])).toBeNull();
    expect(check([file('document.pdf', 10 * MiB + 1)])).toContain('单个附件');
    expect(check([file('image.png', 5 * MiB + 1)])).toContain('单张图片');
    expect(check([file('a.pdf', 10 * MiB), file('b.pdf', 10 * MiB)])).toBeNull();
    expect(check([file('a.pdf', 10 * MiB), file('b.pdf', 10 * MiB), file('c.txt', 1)])).toContain('合计');
    expect(check([file('a.gif', 5 * MiB), file('b.png', 5 * MiB), file('c.jpeg', 1)])).toContain('图片合计');
  });
  it('honors a smaller server limit and preserves the older runtime limit', () => {
    expect(chatAttachmentProblem([file('a.png', 2049)], 'harness', 2048)).toContain('2 KiB');
    expect(chatAttachmentProblem([file('a.png', 6 * MiB)], 'claude', 20 * MiB)).toBeNull();
    expect(chatAttachmentProblem([file('a.pdf', 20 * MiB + 1)], 'claude', 20 * MiB)).toContain('上限');
  });
  it('rejects empty files and unknown limits without pretending to inspect bytes', () => {
    expect(check([file('empty.txt', 0)])).toContain('为空');
    expect(chatAttachmentProblem([file('a.png', 100)], 'harness', NaN)).toContain('暂不可用');
    // Static/animated distinction and real MIME verification belong to the decoder.
    expect(check([file('animation.gif', 100)])).toBeNull();
  });
});
