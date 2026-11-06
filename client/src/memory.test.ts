import { afterEach, describe, expect, it, vi } from 'vitest';
import { deleteMemory, loadMemorySessions, memorySessionQuery, readMemoryFile, readMemoryIndex, writeMemoryFile } from './memory';
import type { MemoryIndex } from './types';

const index:MemoryIndex={conversation_id:'session-one',assistant_id:'shared-assistant',scope:'conversation',revision:5,busy:false,files:[],max_file_bytes:131072,max_total_bytes:1048576};
afterEach(()=>vi.unstubAllGlobals());
describe('independent session memory',()=>{
  it('loads both current and archived sessions with an encoded assistant filter and cancellable request',async()=>{
    const sessions=[{id:'one',archived:false},{id:'two',archived:true}];const fetch=vi.fn().mockResolvedValue(new Response(JSON.stringify(sessions)));vi.stubGlobal('fetch',fetch);const controller=new AbortController();
    expect(await loadMemorySessions('assistant & archived=false',controller.signal)).toEqual(sessions);
    const [url,request]=fetch.mock.calls[0];const query=new URLSearchParams(String(url).split('?')[1]);expect(query.get('assistant_id')).toBe('assistant & archived=false');expect(query.has('archived')).toBe(false);expect(request.signal).toBe(controller.signal);expect(memorySessionQuery('')).toBe('/conversations');
  });
  it('never reads or writes the assistant-wide path and preserves optimistic revision in the selected session',async()=>{
    const file={path:'topics/quality & status.md',content:'会话一的质量要求',revision:5,updated_at:'2026-11-05T10:00:00Z'};const fetch=vi.fn().mockImplementation(()=>Promise.resolve(new Response(JSON.stringify(file))));vi.stubGlobal('fetch',fetch);
    await readMemoryFile('session-one',file.path);await writeMemoryFile('session-two',{path:file.path,content:'会话二的独立要求',revision:2});
    expect(fetch.mock.calls[0][0]).toBe('/api/v1/conversations/session-one/memory/file?path=topics%2Fquality%20%26%20status.md');
    const [url,request]=fetch.mock.calls[1];expect(url).toBe('/api/v1/conversations/session-two/memory/file');expect(request.method).toBe('PUT');expect(JSON.parse(request.body)).toEqual({path:file.path,content:'会话二的独立要求',revision:2});expect(fetch.mock.calls.every(([target])=>!String(target).includes('/assistants/'))).toBe(true);
  });
  it('rejects cross-session or legacy index results instead of displaying a shared memory workspace',async()=>{
    const fetch=vi.fn().mockResolvedValueOnce(new Response(JSON.stringify(index))).mockResolvedValueOnce(new Response(JSON.stringify({...index,conversation_id:'another-session'}))).mockResolvedValueOnce(new Response(JSON.stringify({...index,scope:'legacy_user_assistant'})));vi.stubGlobal('fetch',fetch);
    expect(await readMemoryIndex('session-one')).toEqual(index);await expect(readMemoryIndex('session-one')).rejects.toThrow('不属于所选会话');await expect(readMemoryIndex('session-one')).rejects.toThrow('不属于所选会话');
  });
  it('targets deletion and clearing only at the selected session and forwards the exact revision',async()=>{
    const fetch=vi.fn().mockImplementation(()=>Promise.resolve(new Response(JSON.stringify(index))));vi.stubGlobal('fetch',fetch);
    await deleteMemory('session-one',5,'topics/quality.md');await deleteMemory('session-one',6);
    expect(fetch.mock.calls.map(([url])=>url)).toEqual(['/api/v1/conversations/session-one/memory/file?path=topics%2Fquality.md&revision=5','/api/v1/conversations/session-one/memory?revision=6']);expect(fetch.mock.calls.every(([,request])=>request.method==='DELETE')).toBe(true);
  });
  it('keeps a conflicting draft unchanged and never retries or falls back to shared memory',async()=>{
    const fetch=vi.fn().mockResolvedValue(new Response(JSON.stringify({error:{code:'MEMORY_REVISION_CONFLICT',message:'记忆已更新'}}),{status:409}));vi.stubGlobal('fetch',fetch);const draft={path:'MEMORY.md',content:'待保留的草稿',revision:2};
    await expect(writeMemoryFile('session-one',draft)).rejects.toMatchObject({code:'MEMORY_REVISION_CONFLICT',status:409});expect(fetch).toHaveBeenCalledTimes(1);expect(draft).toEqual({path:'MEMORY.md',content:'待保留的草稿',revision:2});
  });
});
