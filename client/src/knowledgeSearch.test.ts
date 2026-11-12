import { describe, expect, it } from 'vitest';
import { knowledgeDocumentIsIngesting, knowledgeDocumentNeedsReindex, knowledgeGraphThumbnailURL, knowledgeIngestionErrorText, knowledgeIngestionStatusLabel, knowledgeMediaLabel, knowledgeMediaURL, knowledgeRegionStatus, knowledgeSearchPayload } from './KnowledgePage';
import type { KnowledgeDocument, KnowledgeIngestion } from './types';

describe('knowledge search request', () => {
  it('sends an explicit user-selected retrieval mode and bounded context', () => {
    expect(knowledgeSearchPayload('  P-101  ', 'bm25', 128)).toEqual({
      query: 'P-101', limit: 8, mode: 'bm25', context_chars: 128,
    });
  });

  it('labels bounded image regions without claiming visual semantics', () => {
    expect(knowledgeRegionStatus(4, 'needs_model')).toBe('安全网格 4 个区域 · 视觉实体待授权模型');
    expect(knowledgeRegionStatus(undefined, 'needs_model')).toBe('未生成安全区域');
  });

  it('does not duplicate the API prefix for source-bound media URLs', () => {
    expect(knowledgeMediaURL('/api/v1/knowledge-documents/doc/thumbnail?version=1')).toBe('/api/v1/knowledge-documents/doc/thumbnail?version=1');
    expect(knowledgeMediaURL('/knowledge-documents/doc/thumbnail?version=1')).toBe('/api/v1/knowledge-documents/doc/thumbnail?version=1');
    expect(knowledgeMediaLabel('image')).toBe('图片证据');
    expect(knowledgeMediaLabel('page_preview', 3)).toBe('页面预览 · 第 3 页');
  });

  it('builds a version-bound thumbnail URL for graph image evidence nodes', () => {
    expect(knowledgeGraphThumbnailURL('line/name', 2)).toBe('/api/v1/knowledge-documents/line%2Fname/thumbnail?version=2');
  });
});

const ingestion = (patch: Partial<KnowledgeIngestion> = {}): KnowledgeIngestion => ({
  job_id: 'job-1', status: 'running', stage: 'parse', stage_label: '解析文本',
  attempts: 1, max_attempts: 3, retryable: false, error_code: null, error_message: null,
  can_cancel: true, can_retry: false, cancel_requested: false, available_at: null,
  created_at: '2026-11-12T00:00:00Z', updated_at: '2026-11-12T00:00:00Z', finished_at: null,
  events: [], ...patch,
});
const document = (patch: Partial<KnowledgeDocument> = {}): KnowledgeDocument => ({
  id: 'doc-1', kb_id: 'kb-1', name: 'pump.pdf', mime_type: 'application/pdf',
  version: 1, sha256: 'a'.repeat(64), status: 'processing', page_count: 1, chunk_count: 0,
  created_at: '2026-11-12T00:00:00Z', ingestion: ingestion(), ...patch,
});

describe('knowledge ingestion status', () => {
  it('shows parsing, OCR, indexing and bounded attempt progress', () => {
    expect(knowledgeIngestionStatusLabel(ingestion({ stage: 'ocr', stage_label: '光学字符识别', attempts: 2 })))
      .toBe('光学字符识别 · 第 2/3 次尝试');
    expect(knowledgeIngestionStatusLabel(ingestion({ stage: 'embed', stage_label: '建立词法与向量索引' })))
      .toBe('建立词法与向量索引 · 第 1/3 次尝试');
    expect(knowledgeIngestionStatusLabel(ingestion({ status: 'queued', stage: 'retry_wait', attempts: 2 })))
      .toBe('等待重试 · 第 2/3 次尝试后');
    expect(knowledgeIngestionStatusLabel(ingestion({ status: 'succeeded', stage: 'done' }))).toBe('摄取完成');
    expect(knowledgeIngestionStatusLabel(ingestion({ status: 'cancelled' }))).toBe('摄取已取消');
    expect(knowledgeIngestionStatusLabel(null)).toBe('未开始摄取');
  });

  it('distinguishes transient retry from deterministic rejection', () => {
    expect(knowledgeIngestionStatusLabel(ingestion({ status: 'failed', error_code: 'PARSER_UNAVAILABLE', can_retry: true })))
      .toBe('摄取失败 · PARSER_UNAVAILABLE · 可重试');
    expect(knowledgeIngestionStatusLabel(ingestion({ status: 'failed', error_code: 'DOCUMENT_TOO_LARGE', can_retry: false })))
      .toBe('摄取失败 · DOCUMENT_TOO_LARGE · 需修正输入');
  });

  it('polls active jobs and offers retry or cancel only when the server agrees', () => {
    expect(knowledgeDocumentIsIngesting(document())).toBe(true);
    expect(knowledgeDocumentIsIngesting(document({ ingestion: ingestion({ status: 'queued', stage: 'retry_wait' }) }))).toBe(true);
    expect(knowledgeDocumentIsIngesting(document({ ingestion: ingestion({ status: 'succeeded' }) }))).toBe(false);
    expect(knowledgeDocumentIsIngesting(document({ status: 'ready', ingestion: null }))).toBe(false);
    expect(knowledgeDocumentNeedsReindex(document({ status: 'failed', ingestion: null }))).toBe(true);
    expect(knowledgeDocumentNeedsReindex(document({ status: 'ready', ingestion: ingestion({ status: 'cancelled', can_retry: true }) }))).toBe(true);
    expect(knowledgeDocumentNeedsReindex(document({ ingestion: ingestion({ status: 'failed', can_retry: true }) }))).toBe(true);
    expect(knowledgeDocumentNeedsReindex(document({ status: 'failed', ingestion: ingestion({ status: 'failed', error_code: 'DOCUMENT_TOO_LARGE', can_retry: false }) }))).toBe(false);
    expect(knowledgeDocumentNeedsReindex(document({ status: 'needs_ocr', ingestion: ingestion({ status: 'failed', can_retry: false }) }))).toBe(true);
    expect(knowledgeDocumentNeedsReindex(document({ status: 'processing', ingestion: ingestion({ status: 'running' }) }))).toBe(false);
  });

  it('surfaces the ingestion error message before the legacy error field', () => {
    expect(knowledgeIngestionErrorText(document({ error: '旧错误', ingestion: ingestion({ status: 'failed', error_message: '解析器不可用' }) })))
      .toBe('解析器不可用');
    expect(knowledgeIngestionErrorText(document({ error: '旧错误' }))).toBe('旧错误');
    expect(knowledgeIngestionErrorText(document())).toBe('');
  });
});
