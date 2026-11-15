import { LockKeyhole, Users } from 'lucide-react';
import { Field } from './components';
import type { ScopedResource } from './types';

export type ResourceScope = 'private' | 'team';
export const scopeLabel = (scope?: string) => scope === 'private' ? '仅自己' : '团队共享';
export const canEdit = (resource: ScopedResource, admin = false) => resource.can_edit ?? (admin && !resource.scope);
export function ScopeBadge({ scope }: { scope?: string }) { return <span className="scope-badge">{scope === 'private' ? <LockKeyhole size={12}/> : <Users size={12}/>} {scopeLabel(scope)}</span>; }
export function ScopeField({ value, onChange, admin, disabled = false }: { value: ResourceScope; onChange: (value:ResourceScope)=>void; admin:boolean; disabled?:boolean }) {
  return <Field label="配置范围" hint={value === 'private' ? '只有你可以使用和管理。' : '按成员访问权限共享，编辑权限由授权决定。'}><select value={value} disabled={disabled} onChange={e=>onChange(e.target.value as ResourceScope)}><option value="private">仅自己</option>{(admin || value === 'team') && <option value="team" disabled={!admin}>团队共享</option>}</select></Field>;
}
