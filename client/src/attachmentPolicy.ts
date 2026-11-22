import type { ChatAttachmentLimits } from './types';

/** Composer preflight only. Server ownership, content and model checks remain authoritative. */
type AttachmentMetadata = { name: string; size: number };
const MiB = 1024 * 1024;
const DEFAULT_HARNESS_LIMITS: ChatAttachmentLimits = {
  max_files: 10,
  max_file_bytes: 10 * MiB,
  max_image_bytes: 5 * MiB,
  max_total_bytes: 20 * MiB,
  max_image_total_bytes: 10 * MiB,
};
const TEXT = ['.txt', '.md', '.csv', '.json', '.log', '.xml', '.yaml', '.yml', '.html', '.py', '.js', '.ts', '.sql'];
const DOCUMENTS = ['.pdf', '.docx', '.xlsx'];
const IMAGES = ['.png', '.jpg', '.jpeg', '.webp', '.gif'];
export const CHAT_ATTACHMENT_ACCEPT = [...DOCUMENTS, ...TEXT, ...IMAGES].join(',');
const SUPPORTED = new Set([...DOCUMENTS, ...TEXT, ...IMAGES]);
const IMAGE_SUFFIXES = new Set(IMAGES);

function attachmentSizeLimit(bytes: number): string {
  if (bytes % MiB === 0) return `${bytes / MiB} MiB`;
  if (bytes % 1024 === 0) return `${bytes / 1024} KiB`;
  return `${bytes} B`;
}

/** Include existing composer files and all newly selected files in one call. */
export function chatAttachmentProblem(
  files: ReadonlyArray<AttachmentMetadata>, runtimeType: 'harness' | 'claude', serverMaxBytes: number,
  advertisedLimits?: ChatAttachmentLimits, legacyImageMaxBytes?: number,
): string | null {
  const limits = runtimeType === 'harness' ? advertisedLimits ?? DEFAULT_HARNESS_LIMITS : null;
  const maxFiles = limits?.max_files ?? 10;
  if (files.length > maxFiles) return `每条消息最多添加 ${maxFiles} 个附件，请移除部分附件后重试。`;
  if (!Number.isSafeInteger(serverMaxBytes) || serverMaxBytes <= 0) return '附件上传限制暂不可用，请刷新后重试。';
  let total = 0, imageTotal = 0;
  for (const file of files) {
    const suffix = /\.[^.]+$/.exec(file.name)?.[0].toLowerCase() || '';
    if (!SUPPORTED.has(suffix)) return `「${file.name}」暂不支持。请使用文本、PDF、DOCX、XLSX 或静态 PNG/JPEG/WebP/GIF 图片。`;
    if (!Number.isSafeInteger(file.size) || file.size <= 0) return `「${file.name}」为空或大小无效，请检查文件后重试。`;
    const image = IMAGE_SUFFIXES.has(suffix);
    const limit = limits ? Math.min(serverMaxBytes, limits.max_file_bytes, image ? limits.max_image_bytes : Infinity)
      : image && legacyImageMaxBytes ? Math.min(serverMaxBytes, legacyImageMaxBytes) : serverMaxBytes;
    if (file.size > limit) {
      const guidance = runtimeType === 'claude' && image && legacyImageMaxBytes
        ? '请缩小图片，或在支持大图的新会话里发送。' : '请缩小文件后重试。';
      return `「${file.name}」超过${image ? '单张图片' : '单个附件'}上限（${attachmentSizeLimit(limit)}），${guidance}`;
    }
    total += file.size;
    if (image) imageTotal += file.size;
  }
  if (limits && (total > limits.max_total_bytes || imageTotal > limits.max_image_total_bytes)) {
    return `本条消息附件合计最多 ${attachmentSizeLimit(limits.max_total_bytes)}，其中图片合计最多 ${attachmentSizeLimit(limits.max_image_total_bytes)}，请移除或缩小部分附件。`;
  }
  return null;
}
