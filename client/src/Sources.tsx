import { useEffect, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { BookOpen, Database, ExternalLink, Globe, Image as ImageIcon, RefreshCw } from 'lucide-react';
import { api, apiURL, API_BASE, errorText, timeText } from './api';
import { Button, ErrorNotice, Loader, Modal } from './components';
import { knowledgeOriginalURL } from './KnowledgePage';
import type { EvidenceSource } from './types';
import './source-evidence.css';

export const sourceKindLabel={data:'数据来源',knowledge:'知识文件',web:'网页来源'};
const canonicalInternalPath=(value?:string)=>{if(!value)return '';if(value.startsWith('/api/v1/'))return value;if(API_BASE.startsWith('/')&&value.startsWith(`${API_BASE}/`))return `/api/v1${value.slice(API_BASE.length)}`;return '';};
export function sourceIdFromURL(url?:string) { const match=canonicalInternalPath(url)?.match(/^\/api\/v1\/sources\/([A-Za-z0-9_-]{1,128})$/);return match?.[1]||null; }
export function isInternalSourceURL(value?:string) {
  if(!value)return false;
  const origin=typeof window==='undefined'?'https://workspace.invalid':window.location.origin;
  try {
    let url=new URL(value,origin);
    if(url.origin!==origin)return false;
    let path=url.pathname;
    for(let i=0;i<4;i++){let decoded:string;try{decoded=decodeURIComponent(path);}catch{break;}if(decoded===path)break;path=decoded;}
    // A second parse removes dot segments introduced by percent decoding.
    url=new URL(path.replace(/\\/g,'/'),origin);
    return /^\/api\/v1\/sources(?:\/|$)/i.test(canonicalInternalPath(url.pathname));
  } catch{return false;}
}
export function attachmentSourceURL(value?:string,page?:unknown) {if(!value)return null;const canonical=value.startsWith('/api/v1/')?value:value.startsWith(`${API_BASE}/`)?`/api/v1${value.slice(API_BASE.length)}`:'';if(!/^\/api\/v1\/files\/[A-Za-z0-9_-]{1,128}\/content$/.test(canonical))return null;const number=Number(page);return apiURL(canonical)+(Number.isSafeInteger(number)&&number>0?`#page=${number}`:'');}
export function publicWebURL(value?:string) {if(!value)return null;try{const url=new URL(value);return ['https:','http:'].includes(url.protocol)&&!url.username&&!url.password?url.href:null;}catch{return null;}}
// Markdown must never auto-load a third-party image (tracking pixel or
// unvalidated content). Only the application's own authenticated attachment
// endpoint is rendered inline; everything else needs an explicit click.
export function markdownImageURL(value?:string) {return typeof value==='string'?attachmentSourceURL(value):null;}
export function sourceAttributionLabel(source:EvidenceSource) {
  // The shared type may lag the server's attribution enum; compare as text so
  // the label stays correct once purple adds `image_candidate` to types.ts.
  const attribution=source.attribution as string|undefined;
  if(attribution==='search_candidate')return '外部检索候选 · 未核验正文';
  if(attribution==='read')return '网页正文已读取 · 未明确引用';
  if(attribution==='image_candidate')return '图片检索候选 · 未读取网页正文';
  return attribution==='cited'?'回答引用':sourceKindLabel[source.kind];
}
export function isImageSource(source:EvidenceSource){return source.kind==='web'&&source.locator?.capability==='image_search';}
export function isOCRSource(source:EvidenceSource){return source.kind==='knowledge'&&source.locator?.capability==='ocr';}
function locatorText(source:EvidenceSource,key:string){const value=source.locator?.[key];return typeof value==='string'?value:'';}
export const readEvidenceSource = (id:string,signal?:AbortSignal) => api<EvidenceSource>(`/sources/${encodeURIComponent(id)}`,{signal,cache:'no-store'});
export async function openSourceLink(id:string,field:'original_url'|'image_url') {
  // Reserve a tab during the click; never navigate using a cached/model URL.
  const tab=window.open('about:blank','_blank');
  if(!tab)throw new Error('浏览器阻止了新窗口，请允许弹出窗口后重试。');
  tab.opener=null;
  try {
    const source=await readEvidenceSource(id);
    const url=publicWebURL(field==='original_url'?source.original_url:locatorText(source,'image_url'));
    if(!url)throw new Error(field==='original_url'?'该来源没有可打开的公开网页。':'该来源没有可打开的原始图片。');
    tab.location.replace(url);
  } catch(error){tab.close();throw error;}
}
export async function openOriginalSource(id:string) { return openSourceLink(id,'original_url'); }
export async function openOriginalImageSource(id:string) { return openSourceLink(id,'image_url'); }
function OpenSourceButton({id,field='original_url',label='打开原始网页',busyLabel='正在核验权限'}:{id:string;field?:'original_url'|'image_url';label?:string;busyLabel?:string}) {
  const [busy,setBusy]=useState(false),[error,setError]=useState('');
  async function open(){setBusy(true);setError('');try{await openSourceLink(id,field);}catch(e){setError(errorText(e));}finally{setBusy(false);}}
  return <span className="source-open-action"><button type="button" className="button secondary" disabled={busy} onClick={open}><ExternalLink size={13}/>{busy?busyLabel:label}</button>{error&&<span role="alert">{error}</span>}</span>;
}
const locatorLabels:Record<string,string>={scope:'查询范围',file:'文件',row_count:'记录条数',dataset_purpose:'数据集用途',dataset_version:'数据集版本',content_type:'内容类型',character_start:'起始字符位置',character_end:'结束字符位置',truncated:'是否截取',search_query:'搜索词',provider:'检索服务',extraction:'提取方式',start:'片段起始位置',end:'片段结束位置',database:'数据库',schema:'Schema',table:'表',tables:'表',fields:'字段',record_keys:'记录键',filters:'筛选条件',time_range:'数据时间范围',timezone:'时区',queried_at:'取数时间',query_time:'取数时间',aggregation:'统计口径',trace_id:'查询追踪',data_source:'数据源',source_name:'来源名称',knowledge_base:'知识库',kb_name:'知识库',document_name:'文件名',filename:'文件名',version:'文件版本',document_version:'文件版本',sha256:'文件摘要',page:'页码',pages:'页码',section:'章节',paragraph:'段落',paragraph_index:'段落',url:'网页地址',fetched_at:'访问时间',published_at:'发布时间',updated_at:'更新时间',purpose:'用途',missing_fields:'未提供的定位信息',title:'标题',capability:'能力类型',image_url:'原始图片地址',thumbnail_url:'缩略图地址',width:'图片宽度',height:'图片高度',mode:'图片搜索模式',complete:'该页是否完整'};
const renderValue=(value:unknown)=>value===null||value===''?'未提供':typeof value==='object'?JSON.stringify(value,null,2):String(value);
const primaryLocatorKeys=['source_name','data_source','knowledge_base','kb_name','document_name','filename','file','time_range','timezone','queried_at','query_time','filters','scope','aggregation','row_count','page','pages','section','published_at','updated_at','fetched_at','complete','truncated','missing_fields'];

// Query timestamps describe retrieval, not the business date or its timezone.
// Only normalize a complete, explicitly zoned ISO timestamp; retain the original below.
export function sourceQueryTime(value:unknown):string|null {
  if(typeof value!=='string'||value.endsWith('-00:00'))return null;
  const match=value.match(/^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/);
  if(!match)return null;
  const wall=new Date(`${match[1]}T${match[2]}Z`),instant=new Date(value);
  if(!Number.isFinite(wall.getTime())||!Number.isFinite(instant.getTime())||wall.toISOString().slice(0,19)!==`${match[1]}T${match[2]}`)return null;
  return `${instant.toISOString().slice(0,19).replace('T',' ')} UTC`;
}

export function sourceLocalTime(value:unknown):string|null {
  if(!sourceQueryTime(value))return null;
  return `${new Date(value as string).toLocaleString('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23',timeZoneName:'short'})}（本地时间）`;
}

// Web extraction can leave long runs of empty lines. Compact the presentation
// only; the stored excerpt and its character offsets remain untouched.
export function compactWebExcerpt(value:string) {
  return value.replace(/\r\n?/g,'\n')
    .replace(/^(?:[^\S\n]*\n)+/,'')
    .replace(/(?:\n[^\S\n]*)+$/,'')
    .replace(/\n(?:[^\S\n]*\n){2,}/g,'\n\n');
}

// Indent valid JSON without converting its numbers or altering string contents.
export function sourceExcerptView(excerpt:string) {
  const raw=excerpt.trim();
  if(!raw.startsWith('{')&&!raw.startsWith('['))return {structured:false,text:excerpt};
  try{JSON.parse(raw);}catch{return {structured:false,text:excerpt};}
  const tokens=raw.match(/"(?:[^"\\]|\\.)*"|[^\s"{}\[\],:]+|[{}\[\],:]/g)||[];
  let depth=0,text='';
  tokens.forEach((token,index)=>{
    if(token==='{'||token==='['){text+=token;depth++;if(tokens[index+1]!=='}'&&tokens[index+1]!==']')text+='\n'+'  '.repeat(depth);}
    else if(token==='}'||token===']'){depth--;if(tokens[index-1]!=='{'&&tokens[index-1]!=='[')text+='\n'+'  '.repeat(depth);text+=token;}
    else if(token===',')text+=',\n'+'  '.repeat(depth);
    else if(token===':')text+=': ';
    else text+=token;
  });
  return {structured:true,text};
}

export function SourceDetails({source}:{source:EvidenceSource}) {
  const locator=source.locator||{};
  const missing=locator.missing_fields;
  const emptyMissing=missing!==null&&typeof missing==='object'&&Object.keys(missing).length===0;
  const primaryKeys=primaryLocatorKeys.filter(key=>Object.prototype.hasOwnProperty.call(locator,key)&&!(key==='missing_fields'&&emptyMissing));
  const displayValues=new Map<string,unknown>(['queried_at','query_time'].flatMap(key=>{const value=sourceQueryTime(locator[key]);return value?[[key,value] as const]:[];}));
  const fetchedAt=sourceLocalTime(locator.fetched_at);
  if(fetchedAt)displayValues.set('fetched_at',fetchedAt);
  if(locator.aggregation==='database_group_by')displayValues.set('aggregation','按字段分组汇总');
  if(typeof locator.truncated==='boolean')displayValues.set('truncated',locator.truncated?'已截取':'未截取');
  if(typeof locator.complete==='boolean')displayValues.set('complete',locator.complete?'完整':'不完整');
  const primary=primaryKeys.map(key=>[key,displayValues.has(key)?displayValues.get(key):locator[key]] as const);
  const technical=Object.entries(locator).filter(([key])=>!primaryKeys.includes(key)||displayValues.has(key)).map(([key,value])=>[displayValues.has(key)?`${locatorLabels[key]}（原始值）`:key,value] as const);
  const excerpt=sourceExcerptView(source.excerpt||'');
  const displayExcerpt=source.kind==='web'?compactWebExcerpt(excerpt.text):excerpt.text;
  const original=publicWebURL(source.original_url)?source.original_url:undefined;
  const fields=(entries:ReadonlyArray<readonly [string,unknown]>)=><dl className="source-locator">{entries.map(([key,item])=><div key={key}><dt>{locatorLabels[key]||key}</dt><dd><pre title={key==='fetched_at'&&typeof locator.fetched_at==='string'?locator.fetched_at:undefined}>{renderValue(item)}</pre></dd></div>)}</dl>;
  return <div className="source-details">
    {original&&fields([['原始网页',original]])}
    {primary.length>0&&fields(primary)}
    {excerpt.structured?<details className="source-disclosure"><summary>查看原始摘录</summary><pre className="source-excerpt">{excerpt.text}</pre></details>:<><h3 className="source-excerpt-heading">来源内容摘录</h3><pre className="source-excerpt">{displayExcerpt||'此来源未提供内容片段。'}</pre></>}
    <details className="source-disclosure"><summary>技术详情</summary>{fields([['来源 ID',source.id],...technical])}</details>
  </div>;
}
export function answerText(text:string,citations:EvidenceSource[]) {
  // Replace the server's canonical footer with the richer cards, without changing stored text.
  const at=text.lastIndexOf('\n\n来源\n\n');
  if(at>=0&&citations.length){const lines=text.slice(at+'\n\n来源\n\n'.length).trim().split(/\n\n/);const ids=lines.map(line=>line.match(/\]\(\/api\/v1\/sources\/([A-Za-z0-9_-]+)\)$/)?.[1]);if(ids.length===citations.length&&ids.every((id,i)=>id===citations[i].id))text=text.slice(0,at);}
  return text.replace(/\[\[source:[^\]\n]*(?:\]\]|$)/g,'[来源]');
}
function SourceIcon({kind}:{kind:EvidenceSource['kind']}) {return kind==='data'?<Database size={15}/>:kind==='knowledge'?<BookOpen size={15}/>:<Globe size={15}/>;}
function SourceCard({source,index,onOpen}:{source:EvidenceSource;index:number;onOpen:()=>void}) {
  const image=isImageSource(source),ocr=isOCRSource(source);
  return <div className="source-entry" key={source.id}>
    <button type="button" className="source-card" onClick={onOpen}>{image?<ImageIcon size={15}/>:<SourceIcon kind={source.kind}/>}<span><strong>[{source.number||index+1}] {source.title}</strong><small>{sourceAttributionLabel(source)}{source.locator?.page?` · 第 ${String(source.locator.page)} 页`:''}</small>{image&&<small>原网页与原始图片分别核验{locatorText(source,'provider')?` · ${locatorText(source,'provider')}`:''}</small>}{ocr&&<small>附件识别文字 · {locatorText(source,'extraction')||'OCR'}{locatorText(source,'provider')?` · ${locatorText(source,'provider')}`:''}</small>}{ocr&&source.locator?.complete===false&&<small role="status">该页识别不完整，请核对原件</small>}{source.attribution&&source.attribution!=='cited'&&<small>检索记录，不代表回答已据此核验</small>}<small className="source-id">来源 ID：{source.id}</small></span><ExternalLink size={13}/></button>
    {image?<span className="source-image-actions"><OpenSourceButton id={source.id} label="打开原始网页"/><OpenSourceButton id={source.id} field="image_url" label="打开原始图片"/></span>:source.kind==='web'&&<OpenSourceButton id={source.id}/>}
  </div>;
}

type MarkdownNode = {
  type:string; value?:string; url?:string; title?:string|null; children?:MarkdownNode[];
  position?:{start:{offset?:number};end:{offset?:number}};
};

// GFM accepts Chinese sentence punctuation as part of a bare URL. Repair only
// those inferred links, never explicit Markdown/angle links or code. Chinese
// path/query text and balanced brackets remain valid URL content.
function remarkChineseAutolinks() {
  return (tree:MarkdownNode,file:{value:unknown})=>{
    const source=String(file.value);
    const closing:Record<string,string>={'(':')','[':']','{':'}','（':'）','【':'】','《':'》','「':'」','『':'』','“':'”','‘':'’'};
    const closers=new Set(Object.values(closing));
    const split=(node:MarkdownNode):MarkdownNode[]=>{
      const start=node.position?.start.offset,end=node.position?.end.offset;
      const label=node.children?.length===1&&node.children[0].type==='text'?node.children[0].value:undefined;
      if(node.type!=='link'||start===undefined||end===undefined||!label
        ||!/^(?:https?:\/\/|www\.)/i.test(source.slice(start,end))
        ||!/[，。！？；：、）】》」』”’]/u.test(label))return [node];
      const parts:MarkdownNode[]=[];
      const urls=/(?:https?:\/\/|www\.)/gi;
      let cursor=0,match:RegExpExecArray|null;
      while((match=urls.exec(label))!==null){
        const from=match.index,stack:string[]=[];
        let until=from;
        for(;until<label.length;until++){
          const char=label[until];
          if(/[\s<>"，。！？；：、]/u.test(char))break;
          if(closing[char])stack.push(closing[char]);
          else if(closers.has(char)){if(stack.pop()!==char)break;}
        }
        // Match GFM's trailing ASCII punctuation rule for any second link
        // recovered from the originally overlong link.
        while(until>from&&/[.,;!?:]/.test(label[until-1]))until--;
        if(from>cursor)parts.push({type:'text',value:label.slice(cursor,from)});
        const text=label.slice(from,until);
        parts.push({type:'link',url:/^www\./i.test(text)?`http://${text}`:text,children:[{type:'text',value:text}]});
        cursor=until;urls.lastIndex=Math.max(until,from+match[0].length);
      }
      if(cursor<label.length)parts.push({type:'text',value:label.slice(cursor)});
      return parts;
    };
    const visit=(node:MarkdownNode)=>{
      if(!node.children)return;
      node.children=node.children.flatMap(child=>{
        if(child.type==='link')return split(child);
        visit(child);return [child];
      });
    };
    visit(tree);
  };
}

export function AnswerMarkdown({text,citations=[]}:{text:string;citations?:EvidenceSource[]}) {
  const [selected,setSelected]=useState<string|null>(null);
  const clean=answerText(text,citations);
  return <><div className="markdown"><ReactMarkdown remarkPlugins={[remarkGfm,remarkChineseAutolinks]} components={{img:({src,alt,title})=>{
    const safe=markdownImageURL(src);
    return safe?<img src={safe} alt={alt||''} title={title} loading="lazy" referrerPolicy="no-referrer"/>:<span className="source-blocked-image" title="外部图片不会自动加载">[外部图片已阻止自动加载：{alt||'未命名图片'}]</span>;
  },a:({children,href,node:_node,...props})=>{
    const id=sourceIdFromURL(href),verified=id&&citations.some(source=>source.id===id);
    return verified?<button type="button" className="source-inline" onClick={()=>setSelected(id)} title="查看来源详情">{children}</button>:isInternalSourceURL(href)?<span className="source-unverified" title="来源尚未核验">[来源]</span>:<a {...props} href={href} target="_blank" rel="noopener noreferrer">{children}</a>;
  }}}>{clean}</ReactMarkdown></div>
  {citations.length>0&&<div className="answer-sources" aria-label="引用来源"><span>引用与检索记录</span>{citations.map((source,index)=><SourceCard key={source.id} source={source} index={index} onOpen={()=>setSelected(source.id)}/>)}</div>}
  {selected&&<SourceDialog key={selected} id={selected} onClose={()=>setSelected(null)}/>}</>;
}
export function SourceActions({source}:{source:EvidenceSource}) {
  const original=publicWebURL(source.original_url);
  const attachment=attachmentSourceURL(source.content_url,source.locator?.page);
  const image=isImageSource(source);
  if(!source.document_id&&!attachment&&!image&&!original)return null;
  const version=Number(source.document_version||source.locator?.document_version||source.locator?.version)||undefined;
  const page=Number(source.locator?.page)||undefined;
  return <footer className="modal-footer">{source.document_id&&<a className="button primary" href={knowledgeOriginalURL(source.document_id,version,page)} target="_blank" rel="noopener noreferrer"><BookOpen size={16}/>打开引用版本原件</a>}{!source.document_id&&attachment&&<a className="button primary" href={attachment} target="_blank" rel="noopener noreferrer"><BookOpen size={16}/>查看／下载引用附件</a>}{image&&<OpenSourceButton id={source.id} label="打开原始网页" busyLabel="正在核验网页权限"/>}{original&&!image&&<OpenSourceButton id={source.id}/>}{image&&<OpenSourceButton id={source.id} field="image_url" label="打开原始图片" busyLabel="正在核验图片权限"/>}</footer>;
}
export function SourceDialog({id,onClose}:{id:string;onClose:()=>void}) {const [value,setValue]=useState<EvidenceSource|null>(null);const [error,setError]=useState('');const [revision,setRevision]=useState(0);useEffect(()=>{setValue(null);setError('');const controller=new AbortController();readEvidenceSource(id,controller.signal).then(result=>{if(!controller.signal.aborted)setValue(result);}).catch(e=>{if(!controller.signal.aborted)setError(errorText(e));});return()=>controller.abort();},[id,revision]);return <Modal title={value?.title||'来源依据'} description={value?`${sourceKindLabel[value.kind]} · 记录于 ${timeText(value.created_at)}`:undefined} wide onClose={onClose}><div className="modal-body"><ErrorNotice message={error}/>{value?<>{isImageSource(value)&&<p className="source-boundary">当前为图片搜索结果，尚未读取网页正文或下载图片。请按需打开原网页或图片，使用前确认授权。</p>}{isOCRSource(value)&&<p className="source-boundary">文字由外部服务识别，可能存在误差，请核对原件。页码与完整性以识别结果为准。</p>}<SourceDetails source={value}/></>:!error?<Loader/>:<Button variant="secondary" onClick={()=>setRevision(previous=>previous+1)}><RefreshCw size={15}/>重新读取</Button>}</div>{value&&<SourceActions source={value}/>}</Modal>;}
