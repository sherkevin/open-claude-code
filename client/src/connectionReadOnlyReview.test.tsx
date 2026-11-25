import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import ConnectionReadOnlyReview, { connectionReviewPayload, currentReviewedTools } from './ConnectionReadOnlyReview';
import type { Connection } from './types';

const connection: Connection = {id:'c',name:'业务数据',transport:'http',url:'https://example.test/mcp',auth_type:'none',credential_present:false,enabled:true,status:'connected',revision:2,last_error:null,last_tested_at:null,scope:'private',can_edit:true,read_only_review_revision:0,tools:[
  {name:'describe',description:'目录',inputSchema:{type:'object'},definition_digest:'d1',session_read_only_review_eligible:true},
  {name:'query',description:'只读查询',inputSchema:{type:'object'},definition_digest:'d2',session_read_only_review_eligible:true},
  {name:'call',description:'执行动作',inputSchema:{type:'object'},definition_digest:'d3',session_read_only_review_eligible:false,session_read_only_review_reason:'通用动作必须逐次审批'}
]};
const saved: Connection = {...connection,read_only_review_revision:1,read_only_certifications:{describe:{connection_revision:2,definition_digest:'d1',basis:'已核实契约',verified_by:'owner',verified_at:'now'}}};
describe('connection read-only review',()=>{
  it('never selects unreviewed tools automatically and disables generic actions',()=>{
    const html=renderToStaticMarkup(<ConnectionReadOnlyReview connection={connection} onClose={()=>{}} onSaved={()=>{}}/>);
    expect(html).toContain('通用动作必须逐次审批');
    expect(html).not.toContain('checked=""');
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*>保存只读核验/);
  });
  it('preserves an existing review, uses current exact digest for new tools and requires a basis',()=>{
    expect(currentReviewedTools(saved)).toEqual(['describe']);
    expect(connectionReviewPayload(saved,['describe','query'],'已核实查询契约')).toEqual({expected_connection_revision:2,expected_review_revision:1,tools:{describe:{connection_revision:2,definition_digest:'d1',basis:'已核实契约'},query:{connection_revision:2,definition_digest:'d2',basis:'已核实查询契约'}}});
    expect(()=>connectionReviewPayload(saved,['call'],'不要执行')).toThrow();
    expect(()=>connectionReviewPayload(connection,['query'],'')).toThrow();
  });
  it('never treats stale or disabled connections as reviewed',()=>{
    expect(currentReviewedTools({...saved,revision:3})).toEqual([]);
    expect(currentReviewedTools({...saved,status:'failed'})).toEqual([]);
    expect(currentReviewedTools({...saved,enabled:false})).toEqual([]);
  });
});
