import { useEffect, useState } from 'react';
import { api, errorText } from './api';
import { loadAssistantResources, type AssistantResources } from './AssistantConfiguration';
import type { Assistant } from './types';

// Each load owns an AbortController; switching assistants or reloading cannot apply an older response.
export default function useAssistantConfiguration(id:string|null,admin:boolean) {
  const [snapshot,setSnapshot]=useState<{assistant:Assistant|null;resources:AssistantResources}|null>(null);
  const [loading,setLoading]=useState(true);const [error,setError]=useState('');const [generation,setGeneration]=useState(0);
  useEffect(()=>{
    const controller=new AbortController();setLoading(true);setError('');setSnapshot(null);
    Promise.all([id?api<Assistant>(`/assistants/${encodeURIComponent(id)}`,{signal:controller.signal}):Promise.resolve(null),loadAssistantResources(admin,controller.signal)])
      .then(([assistant,resources])=>{if(!controller.signal.aborted)setSnapshot({assistant,resources});})
      .catch(e=>{if(!controller.signal.aborted)setError(errorText(e));})
      .finally(()=>{if(!controller.signal.aborted)setLoading(false);});
    return()=>controller.abort();
  },[id,admin,generation]);
  return {snapshot,loading,error,generation,reload:()=>setGeneration(value=>value+1),saved:(assistant:Assistant)=>setSnapshot(previous=>previous?{...previous,assistant}:previous)};
}
