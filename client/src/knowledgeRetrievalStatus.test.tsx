import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { KnowledgeRetrievalStatus } from './KnowledgePage';
import type { KnowledgeRetrieval } from './types';

const base:KnowledgeRetrieval={mode:'hipporag',lexical:'BM25',vector_model:'fixture',semantic:false,execution:'server-local'};

describe('knowledge retrieval evidence status',()=>{
  it('renders official provenance counts without treating candidate seeds as verified relations',()=>{
    const html=renderToStaticMarkup(<KnowledgeRetrievalStatus retrieval={{...base,algorithm:'hipporag2-official',backend:'official',graph_trace:{algorithm:'hipporag2-official',source_count:4,unique_text_count:3,returned_count:2,seed_nodes:['unverified-candidate']},seed_evidence:[{label:'unverified-fact'}]}}/>);
    expect(html).toContain('HippoRAG 官方检索');
    expect(html).toContain('授权来源 4');
    expect(html).toContain('候选关系未作为已核验事实展示');
    expect(html).not.toContain('unverified-candidate');
    expect(html).not.toContain('unverified-fact');
    expect(html).not.toContain('图路径');
  });

  it('renders fallback as lite rather than an official success',()=>{
    const html=renderToStaticMarkup(<KnowledgeRetrievalStatus retrieval={{...base,algorithm:'hipporag2-lite',backend:'lite',fallback:'identity_mismatch',graph_trace:{seed_count:1,edge_count:2,visited_node_count:3}}}/>);
    expect(html).toContain('role="status"');
    expect(html).toContain('授权文档快照与官方索引不一致');
    expect(html).toContain('不是官方检索结果');
    expect(html).toContain('种子 1 · 扩展实体 3 · 图路径 2');
  });

  it('does not show a HippoRAG panel for ordinary BM25 retrieval',()=>{
    expect(renderToStaticMarkup(<KnowledgeRetrievalStatus retrieval={{...base,mode:'bm25'}}/>)).toBe('');
  });

  it('makes a terminated official timeout explicit instead of claiming official success',()=>{
    const html=renderToStaticMarkup(<KnowledgeRetrievalStatus retrieval={{...base,algorithm:'hipporag2-lite',backend:'lite',fallback:'timeout'}}/>);
    expect(html).toContain('官方检索超过等待期限，本次官方结果未使用');
    expect(html).toContain('不是官方检索结果');
  });
});
