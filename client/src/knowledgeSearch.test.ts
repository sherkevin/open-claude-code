import { describe, expect, it } from 'vitest';
import { knowledgeDocumentNeedsReindex, knowledgeGraphOriginalURL, knowledgeGraphPreviewURL, knowledgeGraphStatusLabel, knowledgeGraphThumbnailURL, knowledgeGraphVisualStatusLabel, knowledgeHitIsHippoRAG, knowledgeHitSourceURL, knowledgeHippoRAGFallbackLabel, knowledgeHippoRAGTraceSummary, knowledgeMediaLabel, knowledgeMediaURL, knowledgeRegionStatus, knowledgeRetrievalModeLabel, knowledgeSearchPayload } from './KnowledgePage';

describe('knowledge search request', () => {
  it('sends an explicit user-selected retrieval mode and bounded context', () => {
    expect(knowledgeSearchPayload('  P-101  ', 'bm25', 128)).toEqual({
      query: 'P-101', limit: 8, mode: 'bm25', context_chars: 128,
    });
  });

  it('supports HippoRAG2-lite mode and keeps graph trace optional', () => {
    expect(knowledgeSearchPayload('  设备故障  ', 'hipporag')).toEqual({
      query: '设备故障', limit: 8, mode: 'hipporag', context_chars: 12000,
    });
    expect(knowledgeRetrievalModeLabel('hipporag')).toBe('HippoRAG2-lite');
    expect(knowledgeHitIsHippoRAG({retrieval_algorithm:'hipporag2-lite'})).toBe(true);
    expect(knowledgeHitIsHippoRAG({retrieval_algorithm:'visual'})).toBe(false);
    expect(knowledgeHippoRAGTraceSummary(undefined)).toBe('');
    expect(knowledgeHippoRAGTraceSummary({seed_entities:[{id:'e1'}],expanded_entities:[{id:'e2'},{id:'e3'}],paths:[{source:'e1',target:'e2'}],iterations:2})).toBe('种子 1 · 扩展实体 2 · 图路径 1 · 2 轮传播');
  });

  it('labels bounded image regions without claiming visual semantics', () => {
    expect(knowledgeRegionStatus(4, 'needs_model')).toBe('安全网格 4 个区域 · 视觉实体待授权模型');
    expect(knowledgeRegionStatus(4, 'failed')).toBe('安全网格 4 个区域 · 解析失败');
    expect(knowledgeRegionStatus(undefined, 'needs_model')).toBe('未生成安全区域');
    expect(knowledgeRegionStatus(undefined, 'failed')).toBe('解析失败 · 未生成安全区域');
  });

  it('distinguishes official retrieval from lite and does not invent verified graph paths', () => {
    expect(knowledgeRetrievalModeLabel('hipporag','hipporag2-official')).toBe('HippoRAG 官方检索');
    expect(knowledgeRetrievalModeLabel('hipporag','unknown')).toBe('HippoRAG2-lite');
    expect(knowledgeHitIsHippoRAG({retrieval_algorithm:'hipporag2-official'})).toBe(true);
    const summary=knowledgeHippoRAGTraceSummary({algorithm:'hipporag2-official',source_count:3,unique_text_count:2,returned_count:3});
    expect(summary).toContain('授权来源 3 · 去重文本 2 · 返回来源 3');
    expect(summary).toContain('候选关系未作为已核验事实展示');
    expect(summary).not.toContain('图路径');
  });

  it('explains safe fallback without rendering arbitrary backend errors', () => {
    expect(knowledgeHippoRAGFallbackLabel('identity_mismatch')).toBe('授权文档快照与官方索引不一致');
    expect(knowledgeHippoRAGFallbackLabel('result_invalid')).toBe('官方结果无法绑定到来源');
    expect(knowledgeHippoRAGFallbackLabel('secret: injected provider error')).toBe('官方检索暂不可用');
    expect(knowledgeHippoRAGFallbackLabel()).toBe('');
  });

  it('does not duplicate the API prefix for source-bound media URLs', () => {
    expect(knowledgeMediaURL('/api/v1/knowledge-documents/doc/thumbnail?version=1')).toBe('/api/v1/knowledge-documents/doc/thumbnail?version=1');
    expect(knowledgeMediaURL('/knowledge-documents/doc/thumbnail?version=1')).toBe('/api/v1/knowledge-documents/doc/thumbnail?version=1');
    expect(knowledgeMediaLabel('image')).toBe('图片证据');
    expect(knowledgeMediaLabel('page_preview', 3)).toBe('页面预览 · 第 3 页');
  });

  it('builds a version-bound thumbnail URL for graph image evidence nodes', () => {
    expect(knowledgeGraphThumbnailURL('line/name', 2)).toBe('/api/v1/knowledge-documents/line%2Fname/thumbnail?version=2');
    expect(knowledgeGraphOriginalURL('line/name', 2)).toBe('/api/v1/knowledge-documents/line%2Fname/content?version=2');
    expect(knowledgeGraphStatusLabel('failed')).toBe('解析失败');
    expect(knowledgeGraphStatusLabel('needs_model')).toBe('视觉模型未授权');
    expect(knowledgeGraphVisualStatusLabel('failed')).toBe('视觉索引失败');
    expect(knowledgeGraphVisualStatusLabel('ready')).toBe('视觉向量已就绪');
  });

  it('keeps image evidence bound to the document version or region URL', () => {
    const region={id:'r1',document_id:'doc',version:3,row:0,column:1,rows:2,columns:2,x:0.5,y:0,width:0.5,height:0.5,thumbnail_width:200,thumbnail_height:100,thumbnail_url:'/knowledge-documents/doc/regions/r1/thumbnail?version=3',visual_index_status:'ready',entity_index_status:'ready'};
    expect(knowledgeGraphPreviewURL('doc',3)).toBe('/api/v1/knowledge-documents/doc/content?version=3');
    expect(knowledgeGraphPreviewURL('doc',3,region)).toBe('/api/v1/knowledge-documents/doc/regions/r1/thumbnail?version=3');
  });

  it('only exposes explicit HTTP(S) remote evidence links',()=>{
    expect(knowledgeHitSourceURL({locator:{url:'https://example.com/report'}})).toBe('https://example.com/report');
    expect(knowledgeHitSourceURL({url:'javascript:alert(1)'})).toBe('');
  });

  it('offers retry when a visual or OCR stage failed even if text parsing is ready',()=>{
    expect(knowledgeDocumentNeedsReindex({status:'ready',image:{visual_index_status:'failed',ocr_status:'ready'}} as never)).toBe(true);
    expect(knowledgeDocumentNeedsReindex({status:'ready',image:{visual_index_status:'ready',ocr_status:'failed'}} as never)).toBe(true);
    expect(knowledgeDocumentNeedsReindex({status:'ready',image:{visual_index_status:'ready',ocr_status:'ready'}} as never)).toBe(false);
  });
});
