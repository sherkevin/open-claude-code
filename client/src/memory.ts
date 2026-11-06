import { api } from './api';
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
export const writeMemoryFile=(conversationId:string,file:Pick<MemoryFile,'path'|'content'|'revision'>)=>api<MemoryFile>(`${memoryEndpoint(conversationId)}/file`,{method:'PUT',body:JSON.stringify(file)});
export async function deleteMemory(conversationId:string,revision:number,path?:string) {
  const suffix=path===undefined?`?revision=${revision}`:`/file?path=${encodeURIComponent(path)}&revision=${revision}`;
  return assertMemoryScope(await api<MemoryIndex>(`${memoryEndpoint(conversationId)}${suffix}`,{method:'DELETE'}),conversationId);
}
