import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import XlsxPreview, { xlsxFilterRows, xlsxFormulaNote, xlsxPage, xlsxSortRows } from './XlsxPreview';
import { WorkflowChatCard, workflowArtifactFile, workflowChatReference, workflowChatStatusLabel, workflowRunIssue } from './WorkflowChat';
import type { StoredFile, WorkflowArtifactPreview, WorkflowRun, XlsxPreview as XlsxPreviewData } from './types';

const run:WorkflowRun={
  id:'run-1234567890',workflow_id:'wf-1',workflow_name:'批次质量汇总',version:3,graph_hash:'h',permission_digest:'p',status:'succeeded',synthetic:true,
  output:{node_outputs:{plan:'模拟计划：检查三个批次',approve:'"workflow_approval"'}},trace:[],created_at:'2026-11-12T01:00:00Z',completed_at:'2026-11-12T01:00:04Z',
};
const preview=(overrides:Partial<XlsxPreviewData>):XlsxPreviewData=>({sheets:[{name:'汇总',columns:['批次','数量'],rows:[['A','2']],row_count:1,truncated:false,formula_cells:0,escaped_text_cells:0}],sheet_count:1,truncated:false,evaluated_formulas:false,formula_cells:0,escaped_text_cells:0,max_rows:200,max_columns:50,...overrides});
const artifact=(overrides:Partial<WorkflowArtifactPreview>):WorkflowArtifactPreview=>({node_id:'excel',kind:'file',format:'xlsx',...overrides});
const file:StoredFile={id:'f1',name:'批次汇总.xlsx',mime_type:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',size:2048,created_at:''};

describe('chat-bound workflow presentation',()=>{
  it('labels every run state and falls back instead of inventing a state',()=>{
    expect(workflowChatStatusLabel('awaiting_approval')).toBe('等待你审批');
    expect(workflowChatStatusLabel('succeeded')).toBe('已完成');
    expect(workflowChatStatusLabel('failed')).toBe('执行失败');
    expect(workflowChatStatusLabel('cancelled')).toBe('已取消');
    expect(workflowChatStatusLabel('weird')).toBe('weird');
    expect(workflowChatStatusLabel('')).toBe('未知状态');
  });
  it('builds a bounded quotation of the real run output and skips approval placeholders',()=>{
    const text=workflowChatReference(run);
    expect(text).toContain('批次质量汇总');
    expect(text).toContain('run-1234');
    expect(text).toContain('> plan: 模拟计划：检查三个批次');
    expect(text).not.toContain('workflow_approval');
    expect(text.split('\n').length).toBeLessThan(8);
    const empty=workflowChatReference({...run,output:{}});
    expect(empty).toContain('结果已写入当前对话');
  });
  it('only exposes artifacts that were really written with a file id and name',()=>{
    expect(workflowArtifactFile(artifact({}))).toBeNull();
    expect(workflowArtifactFile(artifact({written:true,file_id:'f1'}))).toBeNull();
    expect(workflowArtifactFile(artifact({written:true,file_id:'f1',file_name:'批次汇总.xlsx'}))).toEqual({id:'f1',name:'批次汇总.xlsx',mime_type:'application/octet-stream',size:0,created_at:''});
  });
  it('blocks submission only for missing required fields',()=>{
    const fields=[{id:'topic',label:'主题',required:true},{id:'note',label:'备注',required:false}];
    expect(workflowRunIssue(fields,{topic:'批次 A'})).toBe('');
    expect(workflowRunIssue(fields,{topic:'',note:'x'})).toBe('请填写必填字段：主题');
    expect(workflowRunIssue([],{})).toBe('');
  });
  it('renders the loading boundary before any run data arrives',()=>{
    const html=renderToStaticMarkup(<WorkflowChatCard runId="run-1" onReload={()=>{}} onReference={()=>{}}/>);
    expect(html).toContain('正在读取工作流状态');
    expect(html).toContain('workflow-chat-card');
  });
});

describe('Excel interactive preview',()=>{
  const rows=[['批次 B','10','2026-11-02'],['批次 A','2','2026-11-01'],['合计','12','']];
  it('filters across all visible cells and ignores case and surrounding spaces',()=>{
    expect(xlsxFilterRows(rows,'批次 b')).toHaveLength(1);
    expect(xlsxFilterRows(rows,'  ')).toBe(rows);
    expect(xlsxFilterRows(rows,'11-01')).toEqual([rows[1]]);
    expect(xlsxFilterRows(rows,'不存在')).toEqual([]);
  });
  it('sorts numbers numerically and text naturally in both directions',()=>{
    expect(xlsxSortRows(rows,{column:1,direction:'asc'}).map(row=>row[1])).toEqual(['2','10','12']);
    expect(xlsxSortRows(rows,{column:1,direction:'desc'}).map(row=>row[1])).toEqual(['12','10','2']);
    const textAsc=xlsxSortRows(rows,{column:0,direction:'asc'}).map(row=>row[0]);
    const textDesc=xlsxSortRows(rows,{column:0,direction:'desc'}).map(row=>row[0]);
    expect(textAsc).toHaveLength(3);
    expect(textDesc).toEqual([...textAsc].reverse());
    expect(xlsxSortRows(rows,null)).toBe(rows);
  });
  it('pages bounded slices without losing or duplicating rows',()=>{
    const many=Array.from({length:123},(_,index)=>[String(index)]);
    const first=xlsxPage(many,1);const last=xlsxPage(many,3);const over=xlsxPage(many,99);
    expect(first.pageCount).toBe(3);expect(first.rows).toHaveLength(50);expect(last.rows).toHaveLength(23);expect(over.page).toBe(3);
    expect([...first.rows,...xlsxPage(many,2).rows,...last.rows]).toEqual(many);
    expect(xlsxPage([],4)).toEqual({rows:[],page:1,pageCount:1});
  });
  it('states the formula and text-injection boundary instead of implying formulas run',()=>{
    expect(xlsxFormulaNote(preview({formula_cells:2}))).toContain('绝不执行公式');
    expect(xlsxFormulaNote(preview({escaped_text_cells:3}))).toContain('不会被当作公式执行');
    expect(xlsxFormulaNote(preview({}))).toContain('不执行公式或宏');
    expect(xlsxFormulaNote(null)).toBe('');
  });
  it('renders a loading state before the preview request resolves',()=>{
    const html=renderToStaticMarkup(<XlsxPreview file={file}/>);
    expect(html).toContain('xlsx-preview');
    expect(html).toContain('正在读取 Excel 预览');
  });
});
