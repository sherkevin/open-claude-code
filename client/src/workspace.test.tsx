import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { canEdit, ResourceCardHeading, ScopeField } from './resources';
import { AnswerMarkdown, answerText, attachmentSourceURL, isInternalSourceURL, publicWebURL, sourceIdFromURL } from './Sources';
import { knowledgeOriginalURL } from './KnowledgePage';
import { mentionQuery, selectedSkillIds, SkillChips } from './SkillPicker';
import { parseTags, sessionQuery } from './SessionsPage';
import type { EvidenceSource, Skill } from './types';

const skill:Skill={id:'s1',name:'quality-review',description:'检查质量',instructions:'先检查来源',enabled:true,revision:3,required_tools:[],scope:'private',can_edit:true};
describe('resource and session boundaries',()=>{
  it('uses authoritative edit flags even when the viewer is an administrator',()=>{
    expect(canEdit({scope:'private',can_edit:false},true)).toBe(false);
    expect(canEdit({scope:'team',can_edit:false},false)).toBe(false);
    expect(canEdit({scope:'private',can_edit:true},false)).toBe(true);
    const member=renderToStaticMarkup(<ScopeField value="private" admin={false} onChange={()=>{}}/>);
    expect(member).toContain('仅自己');expect(member).not.toContain('value="team"');
  });
  it('encodes query text without allowing text to inject a different assistant or archive filter',()=>{
    const url=sessionQuery({q:'批次&archived=false',assistant_id:'assistant-a',archived:true,category:'生产 质量'});
    const params=new URLSearchParams(url.split('?')[1]);
    expect(params.get('q')).toBe('批次&archived=false');expect(params.get('archived')).toBe('true');expect(params.get('assistant_id')).toBe('assistant-a');expect(params.get('category')).toBe('生产 质量');
    expect(parseTags('质量， 待跟进,质量,,')).toEqual(['质量','待跟进']);
  });
});
describe('explicit skill selection',()=>{
  it('recognizes a mention at the cursor but not an email or arbitrary earlier text',()=>{
    expect(mentionQuery('请用 @quality 检查',11)).toEqual({query:'quality',start:3,end:11});
    expect(mentionQuery('mail@example.com',16)).toBeNull();
    expect(mentionQuery('先 @one 再继续',10)).toBeNull();
    expect(mentionQuery('@',1)).toEqual({query:'',start:0,end:1});
  });
  it('submits only selected currently enabled IDs and never a text path',()=>{
    expect(selectedSkillIds(['s1','s1','/etc/passwd','off'],[skill,{...skill,id:'off',enabled:false}])).toEqual(['s1']);
    const html=renderToStaticMarkup(<SkillChips selected={['s1']} skills={[skill]} disabled={false} onRemove={()=>{}}/>);
    expect(html).not.toContain('lucide-sparkles');
    expect(html).toContain('lucide-x');
    const heading=renderToStaticMarkup(<ResourceCardHeading name={skill.name} scope="private"/>);
    expect(heading).toContain(skill.name);
    expect(heading).toContain('仅自己');
    expect(heading).not.toMatch(/<(?:img|svg)\b/);
    expect(html).toContain('@quality-review');expect(html).not.toContain('v3');expect(html).toContain('type="button"');expect(html).not.toContain('type="submit"');
  });
});
describe('verifiable source rendering',()=>{
  it('intercepts only exact same-service evidence paths and rejects executable or credential URLs',()=>{
    expect(sourceIdFromURL('/api/v1/sources/a_b-12')).toBe('a_b-12');
    for(const value of ['https://evil.example/api/v1/sources/x','/api/v1/sources/../users','/api/v1/sources/x?next=evil','javascript:alert(1)'])expect(sourceIdFromURL(value)).toBeNull();
    for(const value of ['javascript:alert(1)','data:text/html,x','https://name:password@example.com'])expect(publicWebURL(value)).toBeNull();
    expect(publicWebURL('https://example.com/page')).toBe('https://example.com/page');
  });
  it('renders registered citations as source controls and keeps raw source IDs out of draft prose',()=>{
    const source:EvidenceSource={id:'source123',number:1,kind:'knowledge',title:'公司资料',locator:{page:36},excerpt:'公司职责',created_at:'2026-11-05T00:00:00Z',document_id:'doc1',document_version:2};
    const html=renderToStaticMarkup(<AnswerMarkdown text={'公司介绍[1](/api/v1/sources/source123)'} citations={[source]}/>);
    expect(html).toContain('class="source-inline"');expect(html).toContain('知识文件');expect(html).toContain('第 36 页');expect(html).not.toContain('href="/api/v1/sources');
    const draft=renderToStaticMarkup(<AnswerMarkdown text={'内容[[source:private-id]]'}/>);
    expect(draft).toContain('[来源]');expect(draft).not.toContain('private-id');
    expect(answerText('公司介绍[1](/api/v1/sources/source123)\n\n来源\n\n[1] [知识 · 公司资料](/api/v1/sources/source123)',[source])).toBe('公司介绍[1](/api/v1/sources/source123)');
    expect(answerText('内容\n\n来源\n\n用户自己写的来源说明',[source])).toContain('用户自己写的来源说明');
  });
  it('does not enable HTML scripts or dangerous markdown links',()=>{
    const html=renderToStaticMarkup(<AnswerMarkdown text={'<script>alert(1)</script>\n\n[run](javascript:alert%281%29)'}/>);
    expect(html).not.toContain('<script>');expect(html).not.toContain('href="javascript:');
  });
  it('keeps source links inert before finalization or without this answer registered ID',()=>{
    for(const url of ['/api/v1/sources/other','/api/v1/%73ources/other','/api/v1/%2573ources/other','/api/v1/folder/../sources/other','/api/v1/folder/%2e%2e/sources/other','/api/v1/sources/other?x=1#part']) {
      expect(isInternalSourceURL(url)).toBe(true);
      const html=renderToStaticMarkup(<AnswerMarkdown text={`[claimed source](${url})`}/>);
      expect(html).toContain('class="source-unverified"');expect(html).not.toContain('<button');expect(html).not.toContain('<a ');
    }
    const external=renderToStaticMarkup(<AnswerMarkdown text={'[official page](https://example.com/api/v1/sources/article)'}/>);
    expect(external).toContain('href="https://example.com/api/v1/sources/article"');
  });
  it('pins original file links to the actual version and PDF page',()=>{
    expect(knowledgeOriginalURL('doc/1',2,36)).toBe('/api/v1/knowledge-documents/doc%2F1/content?version=2#page=36');
  });
  it('allows only exact authenticated attachment-content paths and numeric page anchors',()=>{
    expect(attachmentSourceURL('/api/v1/files/file_1-a/content',36)).toBe('/api/v1/files/file_1-a/content#page=36');
    expect(attachmentSourceURL('/api/v1/files/file_1-a/content','not-a-page')).toBe('/api/v1/files/file_1-a/content');
    expect(attachmentSourceURL('/api/v1/files/file_1-a/content',-1)).toBe('/api/v1/files/file_1-a/content');
    for(const value of ['https://evil.example/api/v1/files/x/content','//evil.example/x','/api/v1/files/../content','/api/v1/files/x/content?next=evil','/api/v1/files/x/content#page=1','javascript:alert(1)','/api/v1/users/x'])expect(attachmentSourceURL(value,2)).toBeNull();
  });
});
