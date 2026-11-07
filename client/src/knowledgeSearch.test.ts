import { describe, expect, it } from 'vitest';
import { knowledgeGraphThumbnailURL, knowledgeMediaLabel, knowledgeMediaURL, knowledgeRegionStatus, knowledgeSearchPayload } from './KnowledgePage';

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
