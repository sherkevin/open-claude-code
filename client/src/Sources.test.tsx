import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AnswerMarkdown, openOriginalSource } from './Sources';
import type { EvidenceSource } from './types';

afterEach(()=>vi.unstubAllGlobals());
const source=(attribution:EvidenceSource['attribution'],id='web-1'):EvidenceSource=>({id,kind:'web',title:'Industrial reference',attribution,locator:{},excerpt:'',created_at:''});

describe('verified external source visibility',()=>{
  it.each([
    ['cited','回答引用'],['read','网页正文已读取 · 未明确引用'],['search_candidate','外部检索候选 · 未核验正文'],
  ] as const)('keeps %s labels visible after stripping the canonical footer',(attribution,label)=>{
    const html=renderToStaticMarkup(<AnswerMarkdown text={'Answer\n\n来源\n\n[1] [Old footer label](/api/v1/sources/web-1)'} citations={[source(attribution)]}/>);
    expect(html).toContain(label);
    expect(html).not.toContain('Old footer label');
    expect(html).toContain('打开原始网页');
    expect(html).not.toContain('href="https://');
    if(attribution!=='cited')expect(html).toContain('检索记录，不代表回答已据此核验');
  });
  it('preserves mixed internal and external cards',()=>{
    const knowledge={...source('cited','doc-1'),kind:'knowledge' as const,title:'Internal manual'};
    const html=renderToStaticMarkup(<AnswerMarkdown text="Answer" citations={[knowledge,source('search_candidate')]}/>);
    expect(html).toContain('Internal manual');
    expect(html).toContain('Industrial reference');
    expect(html.match(/打开原始网页/g)).toHaveLength(1);
  });
  it('reauthorizes on every click and only opens the URL returned by the registry',async()=>{
    const tab={opener:{},location:{replace:vi.fn()},close:vi.fn()};
    const open=vi.fn().mockReturnValue(tab);
    vi.stubGlobal('window',{open});
    const fetch=vi.fn().mockResolvedValue(new Response(JSON.stringify({...source('read'),original_url:'https://example.com/verified'})));
    vi.stubGlobal('fetch',fetch);
    await openOriginalSource('web-1');
    expect(fetch).toHaveBeenCalledWith('/api/v1/sources/web-1',expect.objectContaining({cache:'no-store',credentials:'include'}));
    expect(tab.opener).toBeNull();
    expect(tab.location.replace).toHaveBeenCalledWith('https://example.com/verified');
    fetch.mockResolvedValueOnce(new Response(JSON.stringify({error:{message:'来源已撤销'}}),{status:404}));
    await expect(openOriginalSource('web-1')).rejects.toThrow('来源已撤销');
    expect(tab.location.replace).toHaveBeenCalledTimes(1);
    expect(tab.close).toHaveBeenCalledTimes(1);
  });
  it.each(['javascript:alert(1)','https://user:password@example.com'])('rejects unsafe registry URL %s',async url=>{
    const tab={opener:{},location:{replace:vi.fn()},close:vi.fn()};
    vi.stubGlobal('window',{open:()=>tab});
    vi.stubGlobal('fetch',vi.fn().mockResolvedValue(new Response(JSON.stringify({...source('read'),original_url:url}))));
    await expect(openOriginalSource('web-1')).rejects.toThrow('公开网页');
    expect(tab.location.replace).not.toHaveBeenCalled();
    expect(tab.close).toHaveBeenCalled();
  });
  it('reports popup blocking instead of pretending navigation succeeded',async()=>{
    const fetch=vi.fn();
    vi.stubGlobal('window',{open:()=>null});vi.stubGlobal('fetch',fetch);
    await expect(openOriginalSource('web-1')).rejects.toThrow('阻止');
    expect(fetch).not.toHaveBeenCalled();
  });
  it('preserves authenticated original links from business data sources',async()=>{
    const tab={opener:{},location:{replace:vi.fn()},close:vi.fn()};
    vi.stubGlobal('window',{open:()=>tab});
    vi.stubGlobal('fetch',vi.fn().mockResolvedValue(new Response(JSON.stringify({...source('cited'),kind:'data',original_url:'https://example.com/report'}))));
    await openOriginalSource('web-1');
    expect(tab.location.replace).toHaveBeenCalledWith('https://example.com/report');
  });
});
