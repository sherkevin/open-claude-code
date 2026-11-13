import { useEffect, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { BookOpen, Database, ExternalLink, Globe } from 'lucide-react';
import { api, apiURL, API_BASE, errorText, timeText } from './api';
import { ErrorNotice, Loader, Modal } from './components';
import { knowledgeOriginalURL } from './KnowledgePage';
import type { EvidenceSource } from './types';

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
const locatorLabels:Record<string,string>={file:'文件',row_count:'记录条数',dataset_purpose:'数据集用途',dataset_version:'数据集版本',content_type:'内容类型',character_start:'起始字符位置',character_end:'结束字符位置',truncated:'是否截取',search_query:'搜索词',provider:'检索服务',extraction:'提取方式',start:'片段起始位置',end:'片段结束位置',database:'数据库',schema:'Schema',table:'表',tables:'表',fields:'字段',record_keys:'记录键',filters:'筛选条件',time_range:'数据时间范围',timezone:'时区',queried_at:'取数时间',query_time:'取数时间',aggregation:'统计口径',trace_id:'查询追踪',data_source:'数据源',source_name:'来源名称',knowledge_base:'知识库',kb_name:'知识库',document_name:'文件名',filename:'文件名',version:'文件版本',document_version:'文件版本',sha256:'文件摘要',page:'页码',pages:'页码',section:'章节',paragraph:'段落',paragraph_index:'段落',url:'网页地址',fetched_at:'访问时间',published_at:'发布时间',updated_at:'更新时间',purpose:'用途',missing_fields:'未提供的定位信息',title:'标题'};
const renderValue=(value:unknown)=>value===null||value===''?'未提供':typeof value==='object'?JSON.stringify(value,null,2):String(value);
export function answerText(text:string,citations:EvidenceSource[]) {
  // Replace the server's canonical footer with the richer cards, without changing stored text.
  const at=text.lastIndexOf('\n\n来源\n\n');
  if(at>=0&&citations.length){const lines=text.slice(at+'\n\n来源\n\n'.length).trim().split(/\n\n/);const ids=lines.map(line=>line.match(/\]\(\/api\/v1\/sources\/([A-Za-z0-9_-]+)\)$/)?.[1]);if(ids.length===citations.length&&ids.every((id,i)=>id===citations[i].id))text=text.slice(0,at);}
  return text.replace(/\[\[source:[^\]\n]*(?:\]\]|$)/g,'[来源]');
}
function SourceIcon({kind}:{kind:EvidenceSource['kind']}) {return kind==='data'?<Database size={15}/>:kind==='knowledge'?<BookOpen size={15}/>:<Globe size={15}/>;}
export function AnswerMarkdown({text,citations=[]}:{text:string;citations?:EvidenceSource[]}) {const [selected,setSelected]=useState<string|null>(null);const clean=answerText(text,citations);return <><div className="markdown"><ReactMarkdown remarkPlugins={[remarkGfm]} components={{a:({children,href,node:_node,...props})=>{const id=sourceIdFromURL(href);const verified=id&&citations.some(source=>source.id===id);return verified?<button type="button" className="source-inline" onClick={()=>setSelected(id)} title="查看来源依据">{children}</button>:isInternalSourceURL(href)?<span className="source-unverified" title="来源尚未核验">[来源]</span>:<a {...props} href={href} target="_blank" rel="noopener noreferrer">{children}</a>;}}}>{clean}</ReactMarkdown></div>{citations.length>0&&<div className="answer-sources" aria-label="引用来源"><span>来源依据</span>{citations.map((source,index)=><button type="button" className="source-card" key={source.id} onClick={()=>setSelected(source.id)}><SourceIcon kind={source.kind}/><span><strong>[{source.number||index+1}] {source.title}</strong><small>{sourceKindLabel[source.kind]}{source.locator?.page?` · 第 ${String(source.locator.page)} 页`:''}</small></span><ExternalLink size={13}/></button>)}</div>}{selected&&<SourceDialog id={selected} onClose={()=>setSelected(null)}/>}</>;}
export function SourceDialog({id,onClose}:{id:string;onClose:()=>void}) {const [value,setValue]=useState<EvidenceSource|null>(null);const [error,setError]=useState('');useEffect(()=>{const controller=new AbortController();api<EvidenceSource>(`/sources/${encodeURIComponent(id)}`,{signal:controller.signal}).then(result=>{if(!controller.signal.aborted)setValue(result);}).catch(e=>{if(!controller.signal.aborted)setError(errorText(e));});return()=>controller.abort();},[id]);const original=publicWebURL(value?.original_url);const attachment=attachmentSourceURL(value?.content_url,value?.locator?.page);const version=Number(value?.document_version||value?.locator?.document_version||value?.locator?.version)||undefined;const page=Number(value?.locator?.page)||undefined;return <Modal title={value?.title||'来源依据'} description={value?`${sourceKindLabel[value.kind]} · 记录于 ${timeText(value.created_at)}`:undefined} wide onClose={onClose}><div className="modal-body"><ErrorNotice message={error}/>{value?<><dl className="source-locator">{Object.entries(value.locator||{}).map(([key,item])=><div key={key}><dt>{locatorLabels[key]||key}</dt><dd><pre>{renderValue(item)}</pre></dd></div>)}</dl><h3 className="source-excerpt-heading">引用时的内容</h3><pre className="source-excerpt">{value.excerpt||'此来源未提供内容片段。'}</pre></>:!error?<Loader/>:null}</div>{value&&<footer className="modal-footer">{value.document_id&&<a className="button primary" href={knowledgeOriginalURL(value.document_id,version,page)} target="_blank" rel="noopener noreferrer"><BookOpen size={16}/>打开引用版本原件</a>}{!value.document_id&&attachment&&<a className="button primary" href={attachment} target="_blank" rel="noopener noreferrer"><BookOpen size={16}/>查看／下载引用附件</a>}{original&&<a className="button secondary" href={original} target="_blank" rel="noopener noreferrer"><ExternalLink size={16}/>打开原始网页</a>}</footer>}</Modal>;}
