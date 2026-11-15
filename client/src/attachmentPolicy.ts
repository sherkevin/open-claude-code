/** Composer preflight only. Server ownership, content and model checks remain authoritative. */
type AttachmentMetadata = { name: string; size: number };
const MiB = 1024 * 1024;
const TEXT = ['.txt', '.md', '.csv', '.json', '.log', '.xml', '.yaml', '.yml', '.html', '.py', '.js', '.ts', '.sql'];
const DOCUMENTS = ['.pdf', '.docx', '.xlsx'];
const IMAGES = ['.png', '.jpg', '.jpeg', '.webp', '.gif'];
export const CHAT_ATTACHMENT_ACCEPT = [...DOCUMENTS, ...TEXT, ...IMAGES].join(',');
const SUPPORTED = new Set([...DOCUMENTS, ...TEXT, ...IMAGES]);
const IMAGE_SUFFIXES = new Set(IMAGES);

/** Include existing composer files and all newly selected files in one call. */
export function chatAttachmentProblem(
  files: ReadonlyArray<AttachmentMetadata>, runtimeType: 'harness' | 'claude', serverMaxBytes: number,
): string | null {
  if (files.length > 10) return '每条消息最多添加 10 个附件，请移除部分附件后重试。';
  if (!Number.isSafeInteger(serverMaxBytes) || serverMaxBytes <= 0) return '附件上传限制暂不可用，请刷新后重试。';
  let total = 0, imageTotal = 0;
  for (const file of files) {
    const suffix = /\.[^.]+$/.exec(file.name)?.[0].toLowerCase() || '';
    if (!SUPPORTED.has(suffix)) return `「${file.name}」暂不支持。请使用文本、PDF、DOCX、XLSX 或静态 PNG/JPEG/WebP/GIF 图片。`;
    if (!Number.isSafeInteger(file.size) || file.size <= 0) return `「${file.name}」为空或大小无效，请检查文件后重试。`;
    const image = IMAGE_SUFFIXES.has(suffix);
    const limit = runtimeType === 'harness' ? Math.min(serverMaxBytes, (image ? 5 : 10) * MiB) : serverMaxBytes;
    if (file.size > limit) return `「${file.name}」超过${image ? '单张图片' : '单个附件'}上限（${Math.round(limit / 1024)} KiB），请缩小文件后重试。`;
    total += file.size;
    if (image) imageTotal += file.size;
  }
  if (runtimeType === 'harness' && (total > 20 * MiB || imageTotal > 10 * MiB)) {
    return '本条消息附件合计最多 20 MiB，其中图片合计最多 10 MiB，请移除或缩小部分附件。';
  }
  return null;
}
