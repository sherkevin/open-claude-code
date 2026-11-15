import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AnswerMarkdown, compactWebExcerpt, openOriginalImageSource, openOriginalSource, readEvidenceSource, SourceActions, SourceDetails, sourceExcerptView, sourceLocalTime, sourceQueryTime } from './Sources';
import type { EvidenceSource } from './types';

afterEach(()=>vi.unstubAllGlobals());
const source=(attribution:EvidenceSource['attribution'],id='web-1'):EvidenceSource=>({id,kind:'web',title:'Industrial reference',attribution,locator:{},excerpt:'',created_at:''});

it('keeps explicitly cited search summaries distinct from fetched page text',()=>{
  const snippet={...source('cited'),locator:{content_type:'search_excerpt'}};
  const page={...source('cited'),locator:{content_type:'page_text'}};
  const text=renderToStaticMarkup(<AnswerMarkdown text="Answer" citations={[snippet]}/>);
  expect(text).toContain('回答引用搜索摘要 · 未核验正文');
  expect(renderToStaticMarkup(<AnswerMarkdown text="Answer" citations={[page]}/>)).toContain('回答引用 · 网页正文已读取');
  expect(renderToStaticMarkup(<SourceDetails source={snippet}/>)).toContain('搜索摘要（未读取网页正文）');
  expect(renderToStaticMarkup(<SourceDetails source={page}/>)).toContain('网页正文摘录');
});

describe('Chinese prose around bare web links',()=>{
  const links=(html:string)=>[...html.matchAll(/<a\b[^>]*href="([^"]*)"[^>]*>(.*?)<\/a>/g)].map(match=>({href:match[1],label:match[2]}));
  it('keeps two actual source URLs separate from surrounding Chinese prose',()=>{
    const first='https://baike.baidu.com/item/RSI%E6%8C%87%E6%A0%87/7459334';
    const second='https://zhuanlan.zhihu.com/p/530618495';
    const text=`百度百科《RSI指标》（${first}）和知乎《常用技术指标之一文读懂RSI指标》（${second}）在本次检索中出现，且摘要内容与上述含义一致，但读取正文时均返回 HTTP 403`;
    const html=renderToStaticMarkup(<AnswerMarkdown text={text}/>);
    expect(links(html)).toEqual([{href:first,label:first},{href:second,label:second}]);
    expect(html.replace(/<[^>]+>/g,'')).toBe(text);
  });
  it('ends bare links at Chinese sentence punctuation without removing the sentence',()=>{
    const html=renderToStaticMarkup(<AnswerMarkdown text="参考 https://example.com/a，正文未读取；另见 https://example.com/b。"/>);
    expect(links(html)).toEqual([{href:'https://example.com/a',label:'https://example.com/a'},{href:'https://example.com/b',label:'https://example.com/b'}]);
    expect(html).toContain('</a>，正文未读取；另见 ');
    expect(html).toContain('</a>。');
  });
  it('preserves Chinese paths, query values, balanced brackets and embedded redirect URLs',()=>{
    const urls=['https://example.com/产品（新版）/库存?q=华微#详情','https://example.com/go?next=https://other.example/中文'];
    const html=renderToStaticMarkup(<AnswerMarkdown text={urls.join('\n\n')}/>);
    expect(links(html).map(link=>decodeURI(link.href))).toEqual(urls);
    expect(links(html).map(link=>link.label)).toEqual(urls);
  });
  it('does not reinterpret punctuation inside explicit, angle or reference links',()=>{
    const url='https://example.com/中文（说明）?q=示例，测试';
    const html=renderToStaticMarkup(<AnswerMarkdown text={`[原文](${url})\n\n<${url}>\n\n[参考][reference]\n\n[reference]: ${url}`}/>);
    expect(links(html).map(link=>decodeURI(link.href))).toEqual([url,url,url]);
    expect(links(html).map(link=>link.label)).toEqual(['原文',url,'参考']);
  });
  it('leaves inline code and fenced code literal',()=>{
    const code='https://example.com/中文）不是链接';
    const html=renderToStaticMarkup(<AnswerMarkdown text={'`'+code+'`\n\n```text\n'+code+'\n```'}/>);
    expect(links(html)).toEqual([]);
    expect(html.match(/不是链接/g)).toHaveLength(2);
  });
});

describe('verified external source visibility',()=>{
  it('retries source reads through the authenticated registry without reusing a failed response',async()=>{
    const fetch=vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({error:{message:'暂时无法读取'}}),{status:503}))
      .mockResolvedValueOnce(new Response(JSON.stringify(source('cited'))));
    vi.stubGlobal('fetch',fetch);
    const first=new AbortController(),retry=new AbortController();
    await expect(readEvidenceSource('web-1',first.signal)).rejects.toThrow('暂时无法读取');
    expect((await readEvidenceSource('web-1',retry.signal)).id).toBe('web-1');
    expect(fetch).toHaveBeenCalledTimes(2);
    for(const [,init] of fetch.mock.calls)expect(init).toMatchObject({cache:'no-store',credentials:'include'});
    expect(fetch.mock.calls[1][1].signal).toBe(retry.signal);
  });
  it.each([
    ['cited','回答引用'],['read','网页正文已读取 · 未明确引用'],['search_candidate','外部检索候选 · 未核验正文'],
  ] as const)('keeps %s labels visible after stripping the canonical footer',(attribution,label)=>{
    const html=renderToStaticMarkup(<AnswerMarkdown text={'Answer\n\n来源\n\n[1] [Old footer label](/api/v1/sources/web-1)'} citations={[source(attribution)]}/>);
    expect(html).toContain(label);
    expect(html).not.toContain('Old footer label');
    expect(html).not.toContain('打开原始网页');
    expect(html.match(/class="source-card"/g)).toHaveLength(1);
    expect(html).not.toContain('href="https://');
    if(attribution!=='cited')expect(html).toContain('检索记录，不代表回答已据此核验');
  });
  it('preserves mixed internal and external cards',()=>{
    const knowledge={...source('cited','doc-1'),kind:'knowledge' as const,title:'Internal manual'};
    const html=renderToStaticMarkup(<AnswerMarkdown text="Answer" citations={[knowledge,source('search_candidate')]}/>);
    expect(html).toContain('Internal manual');
    expect(html).toContain('Industrial reference');
    expect(html).not.toContain('打开原始网页');
    expect(html.match(/<button/g)).toHaveLength(2);
    expect(html).not.toContain('来源 ID');
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
  it('shows image and OCR source boundaries without loading remote images',()=>{
    const image={...source('image_candidate' as EvidenceSource['attribution'],'img-1'),locator:{capability:'image_search',image_url:'https://cdn.example.com/a.jpg',provider:'managed_image_search'}} as EvidenceSource;
    const ocr={...source('cited','ocr-1'),kind:'knowledge' as const,title:'scan.png',locator:{capability:'ocr',page:2,extraction:'managed-ocr',provider:'managed_ocr'}} as EvidenceSource;
    const html=renderToStaticMarkup(<AnswerMarkdown text="Answer" citations={[image,ocr]}/>);
    expect(html).not.toContain('打开原始网页');
    expect(html).not.toContain('打开原始图片');
    const actions=renderToStaticMarkup(<SourceActions source={image}/>);
    expect(actions).toContain('打开原始网页');
    expect(actions).toContain('打开原始图片');
    expect(html).toContain('原网页与原始图片分别核验');
    expect(html).toContain('附件识别文字');
    expect(html).toContain('第 2 页');
    expect(html).not.toContain('src="https://cdn.example.com');
  });
  it('keeps incomplete OCR visible on persisted citation cards',()=>{
    const ocr={...source('cited','ocr-partial'),kind:'knowledge' as const,locator:{capability:'ocr',page:2,complete:false,truncated:true}} as EvidenceSource;
    const html=renderToStaticMarkup(<AnswerMarkdown text="Partial result" citations={[ocr]}/>);
    expect(html).toContain('该页识别不完整，请核对原件');
    expect(html).toContain('第 2 页');
    expect(html).not.toContain('src=');
  });
  it('opens the registry-verified original image, not a model-supplied URL',async()=>{
    const tab={opener:{},location:{replace:vi.fn()},close:vi.fn()};
    vi.stubGlobal('window',{open:()=>tab});
    const payload={...source('image_candidate' as EvidenceSource['attribution'],'img-1'),locator:{capability:'image_search',image_url:'https://cdn.example.com/verified.jpg'}};
    vi.stubGlobal('fetch',vi.fn().mockResolvedValue(new Response(JSON.stringify(payload))));
    await openOriginalImageSource('img-1');
    expect(tab.location.replace).toHaveBeenCalledWith('https://cdn.example.com/verified.jpg');
  });
  it('blocks third-party markdown images but keeps authenticated attachments',()=>{
    const remote=renderToStaticMarkup(<AnswerMarkdown text="![tracker](https://tracker.invalid/pixel.png)"/>);
    expect(remote).toContain('外部图片已阻止自动加载');
    expect(remote).not.toContain('tracker.invalid');
    const local=renderToStaticMarkup(<AnswerMarkdown text="![scan](/api/v1/files/file-1/content)"/>);
    expect(local).toContain('src="/api/v1/files/file-1/content"');
  });
});


describe('business-readable source details',()=>{
  it('shows existing data and query times before collapsed technical fields',()=>{
    const value={...source('cited'),kind:'data' as const,locator:{database:'orders_db',trace_id:'trace-1',time_range:'2026-11-14',queried_at:'2026-11-15T05:17:29Z',filters:{region:'全部'}},excerpt:'{"rows":[{"amount":123.4}]}'};
    const html=renderToStaticMarkup(<SourceDetails source={value}/>);
    const visible=html.slice(0,html.indexOf('<details'));
    expect(visible).toContain('数据时间范围');
    expect(visible).toContain('2026-11-14');
    expect(visible).toContain('取数时间');
    expect(visible).toContain('2026-11-15 05:17:29 UTC');
    expect(visible).not.toContain('2026-11-15T05:17:29Z');
    expect(html.slice(html.indexOf('<details'))).toContain('2026-11-15T05:17:29Z');
    expect(visible).toContain('全部');
    expect(visible).not.toContain('orders_db');
    expect(visible).not.toContain('trace-1');
    expect(html).toContain('<summary>查看原始摘录</summary>');
    expect(html).toContain('<summary>技术详情</summary>');
    expect(html).toContain('orders_db');
    expect(html).toContain('trace-1');
    expect(html).not.toContain('open=""');
    expect(html).not.toContain('记录条数');
  });
  it('keeps ordinary and malformed text excerpts immediately readable',()=>{
    for(const excerpt of ['第一行原文。\n第二行原文。','{"rows": 截取至此']){
      const html=renderToStaticMarkup(<SourceDetails source={{...source('cited'),excerpt}}/>);
      expect(html.slice(0,html.indexOf('<details'))).toContain('来源内容摘录');
      expect(html).not.toContain('查看原始摘录');
      expect(sourceExcerptView(excerpt)).toEqual({structured:false,text:excerpt});
    }
  });
  it('preserves raw numbers, escaped strings and empty structures when indenting JSON',()=>{
    const excerpt=String.raw`{"id":9007199254740993,"amount":123.4500,"ratio":1e-09,"text":"keep  spaces, [brackets] and \"quotes\"","rows":[],"extra":{}}`;
    const result=sourceExcerptView(excerpt);
    expect(result.structured).toBe(true);
    expect(result.text).toContain('9007199254740993');
    expect(result.text).toContain('123.4500');
    expect(result.text).toContain('1e-09');
    expect(result.text).toContain('keep  spaces, [brackets]');
    expect(result.text).toContain('"rows": []');
    expect(result.text).toContain('"extra": {}');
    expect(JSON.parse(result.text)).toEqual(JSON.parse(excerpt));
  });
  it('does not invent missing time, count or scope and keeps an empty excerpt explicit',()=>{
    const html=renderToStaticMarkup(<SourceDetails source={source('cited')}/>);
    expect(html).toContain('此来源未提供内容片段。');
    expect(html).not.toContain('数据时间范围');
    expect(html).not.toContain('查询范围');
    expect(html).not.toContain('记录条数');
  });
  it('retains incomplete markers and unknown locator fields without treating them as business facts',()=>{
    const value={...source('cited'),locator:{complete:false,truncated:true,missing_fields:['date'],internal_vendor_field:'original-value'},excerpt:'原文'};
    const html=renderToStaticMarkup(<SourceDetails source={value}/>);
    const visible=html.slice(0,html.indexOf('<details'));
    expect(visible).toContain('该页是否完整');
    expect(visible).toContain('不完整');
    expect(visible).not.toContain('false');
    expect(visible).toContain('是否截取');
    expect(visible).toContain('已截取');
    expect(html.slice(html.indexOf('<details'))).toContain('false');
    expect(visible).toContain('未提供的定位信息');
    expect(visible).not.toContain('internal_vendor_field');
    expect(html).toContain('internal_vendor_field');
    expect(html).toContain('original-value');
  });
});


describe('source detail presentation refinements',()=>{
  it('compacts blank lines in web excerpts without changing evidence, offsets or nonempty indentation',()=>{
    const excerpt='\r\n \t\r\n  第一段  保留内部空格\r\n\r\n \t\r\n\r\n    第二段\r\n \t\r\n';
    const locator=Object.freeze({character_start:140,character_end:280,truncated:false});
    const value=Object.freeze({...source('read'),locator,excerpt});
    const html=renderToStaticMarkup(<SourceDetails source={value}/>);
    expect(html).toContain('  第一段  保留内部空格\n\n    第二段');
    expect(html).not.toMatch(/\n\s*\n\s*\n/);
    expect(html).toContain('140');
    expect(html).toContain('280');
    expect(value.excerpt).toBe(excerpt);
    expect(value.locator).toBe(locator);
    const internal=renderToStaticMarkup(<SourceDetails source={{...value,kind:'knowledge'}}/>);
    expect(internal).toContain(excerpt);
    expect(compactWebExcerpt('一行\n\n下一段')).toBe('一行\n\n下一段');
  });
  it('shows boolean markers in Chinese and keeps exact boolean values in technical details',()=>{
    const html=renderToStaticMarkup(<SourceDetails source={{...source('read'),locator:{truncated:false,complete:true}}}/>);
    const visible=html.slice(0,html.indexOf('<details'));
    expect(visible).toContain('未截取');
    expect(visible).toContain('完整');
    expect(visible).not.toMatch(/true|false/);
    const technical=html.slice(html.indexOf('<details'));
    expect(technical).toContain('是否截取（原始值）');
    expect(technical).toContain('false');
    expect(technical).toContain('true');
  });
  it('formats fetched_at for the viewer while preserving its exact zoned original',()=>{
    const raw='2026-11-15T13:26:54.488630+08:00';
    expect(sourceLocalTime(raw)).toBe(sourceLocalTime('2026-11-15T05:26:54.488630Z'));
    const html=renderToStaticMarkup(<SourceDetails source={{...source('read'),locator:{fetched_at:raw}}}/>);
    const visible=html.slice(0,html.indexOf('<details'));
    expect(visible).toContain('访问时间');
    expect(visible).toContain('（本地时间）');
    expect(visible).toContain(`title="${raw}"`);
    expect(visible).not.toContain(`>${raw}<`);
    expect(html.slice(html.indexOf('<details'))).toContain('访问时间（原始值）');
    expect(html.slice(html.indexOf('<details'))).toContain(raw);
    for(const value of ['2026-11-15','2026-11-15T05:26:54','2026-02-30T05:26:54Z','yesterday'])expect(sourceLocalTime(value)).toBeNull();
    const unzoned=renderToStaticMarkup(<SourceDetails source={{...source('read'),locator:{fetched_at:'2026-11-15T05:26:54'}}}/>);
    expect(unzoned).toContain('>2026-11-15T05:26:54<');
    expect(unzoned).not.toContain('（本地时间）');
  });
  it('displays the exact original web address without adding a link that bypasses reauthorization',()=>{
    const original='https://example.com/report/%E4%BE%8B?item=1&version=2#section';
    const html=renderToStaticMarkup(<SourceDetails source={{...source('read'),original_url:original}}/>);
    const visible=html.slice(0,html.indexOf('<details'));
    expect(visible).toContain('原始网页');
    expect(visible).toContain('https://example.com/report/%E4%BE%8B?item=1&amp;version=2#section');
    expect(html).not.toContain('href=');
    expect(html).not.toContain('src=');
    for(const unsafe of ['javascript:alert(1)','https://user:secret@example.com']){
      const invalid=renderToStaticMarkup(<SourceDetails source={{...source('read'),original_url:unsafe}}/>);
      expect(invalid).not.toContain('原始网页');
      expect(invalid).not.toContain('secret');
    }
  });
  it('shows retrieval timestamps in explicit UTC while preserving business dates, zones and raw precision',()=>{
    const raw='2026-11-15T13:26:54.488630+08:00';
    const html=renderToStaticMarkup(<SourceDetails source={{...source('cited'),locator:{queried_at:raw,query_time:'2026-11-15T05:26:54Z',time_range:'2026-11-14',timezone:'Asia/Shanghai'}}}/>);
    const visible=html.slice(0,html.indexOf('<details'));
    expect(visible).toContain('2026-11-15 05:26:54 UTC');
    expect(visible).toContain('2026-11-14');
    expect(visible).toContain('Asia/Shanghai');
    expect(visible).not.toContain(raw);
    expect(html.slice(html.indexOf('<details'))).toContain('取数时间（原始值）');
    expect(html.slice(html.indexOf('<details'))).toContain(raw);
  });
  it('does not invent a timezone for partial, invalid or unzoned retrieval values',()=>{
    for(const value of ['2026-11-15','2026-11-15T05:26:54','2026-11-15T05:26:54-00:00','2026-02-30T05:26:54Z','2026-11-15T25:26:54Z','yesterday',null,123])expect(sourceQueryTime(value)).toBeNull();
    const html=renderToStaticMarkup(<SourceDetails source={{...source('cited'),locator:{queried_at:'2026-11-15T05:26:54'}}}/>);
    expect(html.slice(0,html.indexOf('<details'))).toContain('2026-11-15T05:26:54');
    expect(html).not.toContain(' UTC');
  });
  it('labels the known aggregation in Chinese and preserves its raw value in technical details',()=>{
    const html=renderToStaticMarkup(<SourceDetails source={{...source('cited'),locator:{aggregation:'database_group_by'}}}/>);
    const visible=html.slice(0,html.indexOf('<details'));
    expect(visible).toContain('按字段分组汇总');
    expect(visible).not.toContain('database_group_by');
    expect(html.slice(html.indexOf('<details'))).toContain('database_group_by');
    const unknown=renderToStaticMarkup(<SourceDetails source={{...source('cited'),locator:{aggregation:'vendor_custom'}}}/>);
    expect(unknown.slice(0,unknown.indexOf('<details'))).toContain('vendor_custom');
  });
  it.each([[],{}])('keeps empty missing fields %j in technical details only',missing_fields=>{
    const html=renderToStaticMarkup(<SourceDetails source={{...source('cited'),locator:{missing_fields}}}/>);
    expect(html.slice(0,html.indexOf('<details'))).not.toContain('未提供的定位信息');
    expect(html.slice(html.indexOf('<details'))).toContain('未提供的定位信息');
  });
  it('does not render an empty footer or unsafe original link',()=>{
    expect(renderToStaticMarkup(<SourceActions source={source('cited')}/>)).toBe('');
    expect(renderToStaticMarkup(<SourceActions source={{...source('cited'),original_url:'javascript:alert(1)'}}/>)).toBe('');
  });
  it('retains actual attachment and authenticated external-link actions',()=>{
    const attachment=renderToStaticMarkup(<SourceActions source={{...source('cited'),content_url:'/api/v1/files/file-1/content'}}/>);
    expect(attachment).toContain('modal-footer');
    expect(attachment).toContain('查看／下载引用附件');
    const external=renderToStaticMarkup(<SourceActions source={{...source('cited'),original_url:'https://example.com/report'}}/>);
    expect(external).toContain('打开原始网页');
    expect(external).not.toContain('href="https://example.com/report"');
  });
});
