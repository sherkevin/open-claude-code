import { useEffect, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { BookOpen, Database, ExternalLink, Globe, Image as ImageIcon } from 'lucide-react';
import { api, apiURL, API_BASE, errorText, timeText } from './api';
import { ErrorNotice, Loader, Modal } from './components';
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
export async function openSourceLink(id:string,field:'original_url'|'image_url') {
  // Reserve a tab during the click; never navigate using a cached/model URL.
  const tab=window.open('about:blank','_blank');
  if(!tab)throw new Error('浏览器阻止了新窗口，请允许弹出窗口后重试。');
  tab.opener=null;
  try {
    const source=await api<EvidenceSource>(`/sources/${encodeURIComponent(id)}`,{cache:'no-store'});
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
const locatorLabels:Record<string,string>={file:'文件',row_count:'记录条数',dataset_purpose:'数据集用途',dataset_version:'数据集版本',content_type:'内容类型',character_start:'起始字符位置',character_end:'结束字符位置',truncated:'是否截取',search_query:'搜索词',provider:'检索服务',extraction:'提取方式',start:'片段起始位置',end:'片段结束位置',database:'数据库',schema:'Schema',table:'表',tables:'表',fields:'字段',record_keys:'记录键',filters:'筛选条件',time_range:'数据时间范围',timezone:'时区',queried_at:'取数时间',query_time:'取数时间',aggregation:'统计口径',trace_id:'查询追踪',data_source:'数据源',source_name:'来源名称',knowledge_base:'知识库',kb_name:'知识库',document_name:'文件名',filename:'文件名',version:'文件版本',document_version:'文件版本',sha256:'文件摘要',page:'页码',pages:'页码',section:'章节',paragraph:'段落',paragraph_index:'段落',url:'网页地址',fetched_at:'访问时间',published_at:'发布时间',updated_at:'更新时间',purpose:'用途',missing_fields:'未提供的定位信息',title:'标题',capability:'能力类型',image_url:'原始图片地址',thumbnail_url:'缩略图地址',width:'图片宽度',height:'图片高度',mode:'图片搜索模式',complete:'该页是否完整'};
const renderValue=(value:unknown)=>value===null||value===''?'未提供':typeof value==='object'?JSON.stringify(value,null,2):String(value);
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
    <button type="button" className="source-card" onClick={onOpen}>{image?<ImageIcon size={15}/>:<SourceIcon kind={source.kind}/>}<span><strong>[{source.number||index+1}] {source.title}</strong><small>{sourceAttributionLabel(source)}{source.locator?.page?` · 第 ${String(source.locator.page)} 页`:''}</small>{image&&<small>原网页与原始图片分别核验{locatorText(source,'provider')?` · ${locatorText(source,'provider')}`:''}</small>}{ocr&&<small>附件识别文字 · {locatorText(source,'extraction')||'OCR'}{locatorText(source,'provider')?` · ${locatorText(source,'provider')}`:''}</small>}{source.attribution&&source.attribution!=='cited'&&<small>检索记录，不代表回答已据此核验</small>}<small className="source-id">来源 ID：{source.id}</small></span><ExternalLink size={13}/></button>
    {image?<span className="source-image-actions"><OpenSourceButton id={source.id} label="打开原始网页"/><OpenSourceButton id={source.id} field="image_url" label="打开原始图片"/></span>:source.kind==='web'&&<OpenSourceButton id={source.id}/>}
  </div>;
}
export function AnswerMarkdown({text,citations=[]}:{text:string;citations?:EvidenceSource[]}) {
  const [selected,setSelected]=useState<string|null>(null);
  const clean=answerText(text,citations);
  return <><div className="markdown"><ReactMarkdown remarkPlugins={[remarkGfm]} components={{img:({src,alt,title})=>{
    const safe=markdownImageURL(src);
    return safe?<img src={safe} alt={alt||''} title={title} loading="lazy" referrerPolicy="no-referrer"/>:<span className="source-blocked-image" title="外部图片不会自动加载">[外部图片已阻止自动加载：{alt||'未命名图片'}]</span>;
  },a:({children,href,node:_node,...props})=>{
    const id=sourceIdFromURL(href),verified=id&&citations.some(source=>source.id===id);
    return verified?<button type="button" className="source-inline" onClick={()=>setSelected(id)} title="查看来源详情">{children}</button>:isInternalSourceURL(href)?<span className="source-unverified" title="来源尚未核验">[来源]</span>:<a {...props} href={href} target="_blank" rel="noopener noreferrer">{children}</a>;
  }}}>{clean}</ReactMarkdown></div>
  {citations.length>0&&<div className="answer-sources" aria-label="引用来源"><span>引用与检索记录</span>{citations.map((source,index)=><SourceCard key={source.id} source={source} index={index} onOpen={()=>setSelected(source.id)}/>)}</div>}
  {selected&&<SourceDialog key={selected} id={selected} onClose={()=>setSelected(null)}/>}</>;
}
export function SourceDialog({id,onClose}:{id:string;onClose:()=>void}) {const [value,setValue]=useState<EvidenceSource|null>(null);const [error,setError]=useState('');useEffect(()=>{setValue(null);setError('');const controller=new AbortController();api<EvidenceSource>(`/sources/${encodeURIComponent(id)}`,{signal:controller.signal,cache:'no-store'}).then(result=>{if(!controller.signal.aborted)setValue(result);}).catch(e=>{if(!controller.signal.aborted)setError(errorText(e));});return()=>controller.abort();},[id]);const original=publicWebURL(value?.original_url);const attachment=attachmentSourceURL(value?.content_url,value?.locator?.page);const image=value?isImageSource(value):false;const version=Number(value?.document_version||value?.locator?.document_version||value?.locator?.version)||undefined;const page=Number(value?.locator?.page)||undefined;return <Modal title={value?.title||'来源依据'} description={value?`${sourceKindLabel[value.kind]} · 记录于 ${timeText(value.created_at)}`:undefined} wide onClose={onClose}><div className="modal-body"><ErrorNotice message={error}/>{value?<>{isImageSource(value)&&<p className="source-boundary">图片候选只证明检索服务返回了原网页与原图链接，不代表网页正文已读取、图片已下载或版权可用。预览不会自动加载第三方图片，请按需打开。</p>}{isOCRSource(value)&&<p className="source-boundary">OCR 文字来自外部识别服务，是未受信任资料；页码与完整性来自识别结果，缺页或截断不会显示为全文成功。</p>}<dl className="source-locator">{Object.entries(value.locator||{}).map(([key,item])=><div key={key}><dt>{locatorLabels[key]||key}</dt><dd><pre>{renderValue(item)}</pre></dd></div>)}</dl><h3 className="source-excerpt-heading">来源内容摘录</h3><pre className="source-excerpt">{value.excerpt||'此来源未提供内容片段。'}</pre></>:!error?<Loader/>:null}</div>{value&&<footer className="modal-footer">{value.document_id&&<a className="button primary" href={knowledgeOriginalURL(value.document_id,version,page)} target="_blank" rel="noopener noreferrer"><BookOpen size={16}/>打开引用版本原件</a>}{!value.document_id&&attachment&&<a className="button primary" href={attachment} target="_blank" rel="noopener noreferrer"><BookOpen size={16}/>查看／下载引用附件</a>}{image&&<OpenSourceButton id={id} label="打开原始网页" busyLabel="正在核验网页权限"/>}{original&&!image&&<OpenSourceButton id={id}/>}{image&&<OpenSourceButton id={id} field="image_url" label="打开原始图片" busyLabel="正在核验图片权限"/>}</footer>}</Modal>;}
