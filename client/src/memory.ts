import { api, clientRequestId } from './api';
import type { Conversation, MemoryFile, MemoryIndex } from './types';

export const memoryEndpoint=(conversationId:string)=>`/conversations/${encodeURIComponent(conversationId)}/memory`;
export function memorySessionQuery(assistantId:string) {
  const params=new URLSearchParams();if(assistantId)params.set('assistant_id',assistantId);
  // Omit archived so both current and archived sessions are returned.
  return `/conversations${params.size?`?${params}`:''}`;
}
export async function loadMemorySessions(assistantId:string,signal?:AbortSignal) {
  return api<Conversation[]>(memorySessionQuery(assistantId),{signal});
}
function assertMemoryScope(value:MemoryIndex,conversationId:string):MemoryIndex {
  if(value.scope!=='conversation'||value.conversation_id!==conversationId)throw new Error('返回的记忆不属于所选会话，请刷新后重试。');
  return value;
}
export async function readMemoryIndex(conversationId:string) {return assertMemoryScope(await api<MemoryIndex>(memoryEndpoint(conversationId)),conversationId);}
export const readMemoryFile=(conversationId:string,path:string)=>api<MemoryFile>(`${memoryEndpoint(conversationId)}/file?path=${encodeURIComponent(path)}`);
export const writeMemoryFile=(conversationId:string,file:Pick<MemoryFile,'path'|'content'|'revision'>,operationId?:string)=>api<MemoryFile>(`${memoryEndpoint(conversationId)}/file`,{method:'PUT',body:JSON.stringify({...file,...(operationId?{operation_id:operationId}:{})})});
export async function deleteMemory(conversationId:string,revision:number,path?:string,operationId?:string) {
  const suffix=path===undefined?`?revision=${revision}`:`/file?path=${encodeURIComponent(path)}&revision=${revision}`;
  const operation=operationId?`&operation_id=${encodeURIComponent(operationId)}`:'';
  return assertMemoryScope(await api<MemoryIndex>(`${memoryEndpoint(conversationId)}${suffix}${operation}`,{method:'DELETE'}),conversationId);
}

export interface MemoryMutationKey { signature:string; id:string }
export interface PendingMemoryMutation { operation_id:string; conversation_id:string; kind:'save'|'file'|'all'; path:string; revision:number }
export interface MemoryOperationStatus { operation_id:string; status:string; result?:{revision?:number;native_epoch?:number;context_reset?:boolean;mutation_status?:string}|null }
const pendingKey=(conversationId:string)=>`zhixing.memory.pending.${conversationId}`;
export function readPendingMemoryMutation(conversationId:string):PendingMemoryMutation|null {
  const raw=sessionStorage.getItem(pendingKey(conversationId));
  if(!raw)return null;
  const value=JSON.parse(raw) as PendingMemoryMutation;
  if(value.conversation_id!==conversationId||!['save','file','all'].includes(value.kind)||typeof value.operation_id!=='string'||!value.operation_id||value.operation_id.length>128||typeof value.path!=='string'||value.path.length>240||!Number.isSafeInteger(value.revision)||value.revision<0)throw new Error('浏览器中的待确认操作记录无效，请勿重复修改记忆。');
  return value;
}
export function persistPendingMemoryMutation(value:PendingMemoryMutation) {
  // Deliberately exclude the draft body: only recovery metadata survives reload.
  const {operation_id,conversation_id,kind,path,revision}=value;
  try { sessionStorage.setItem(pendingKey(conversation_id),JSON.stringify({operation_id,conversation_id,kind,path,revision})); }
  catch { throw new Error('浏览器无法保存操作编号，本次未发送修改；请允许会话存储后重试。'); }
}
export function clearPendingMemoryMutation(conversationId:string) {sessionStorage.removeItem(pendingKey(conversationId));}
export const readMemoryOperation=(conversationId:string,operationId:string)=>api<MemoryOperationStatus>(`${memoryEndpoint(conversationId)}/operations/${encodeURIComponent(operationId)}`);
// Keep an uncertain mutation's key until success; a lost response must not apply it twice.
export function memoryMutationKey(previous:MemoryMutationKey|null,payload:unknown):MemoryMutationKey {
  const signature=JSON.stringify(payload);
  return previous?.signature===signature?previous:{signature,id:clientRequestId()};
}
export function memoryMutationMessage(kind:'save'|'file'|'all',result:{context_reset?:boolean;mutation_status?:string}) {
  const action=result.mutation_status==='unchanged'?'记忆内容未变化。':kind==='save'?'记忆文件已保存。':kind==='all'?'此会话的当前记忆已清空。':'记忆文件已删除。';
  return action+(result.context_reset?'下一次执行将使用新的上下文，不再自动带入旧消息、旧记忆和旧附件。页面历史与审计记录仍保留。':'');
}
