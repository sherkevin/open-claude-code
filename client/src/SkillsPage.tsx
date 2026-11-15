import { useCallback, useEffect, useState } from 'react';
import { BookOpen, ChevronDown, FileText, Pencil, Plus, Sparkles, Trash2, Upload, Wrench } from 'lucide-react';
import { api, errorText, patch, post, remove } from './api';
import { Button, EmptyState, ErrorNotice, Field, Loader, Modal, PageHeader, SuccessNotice } from './components';
import { canEdit, ScopeBadge, ScopeField, type ResourceScope } from './resources';
import MarkdownDocument from './MarkdownDocument';
import type { Skill } from './types';
import './skill-detail.css';

export default function SkillsPage({ admin }: { admin: boolean }) {
  const [items, setItems] = useState<Skill[]>([]), [loading, setLoading] = useState(true), [error, setError] = useState(''), [success, setSuccess] = useState('');
  const [editor, setEditor] = useState<Skill | 'new' | null>(null), [importing, setImporting] = useState(false), [history, setHistory] = useState<Skill | null>(null), [deleting, setDeleting] = useState<Skill | null>(null), [busy, setBusy] = useState(false);
  const load = useCallback(async () => { try { setItems(await api<Skill[]>('/skills')); setError(''); } catch (cause) { setError(errorText(cause)); } finally { setLoading(false); } }, []);
  useEffect(() => { void load(); }, [load]);
  return <div className="page skills-catalog"><PageHeader eyebrow="" title="Skills" description="查看与维护业务处理指引。" actions={<><Button variant="secondary" onClick={() => setImporting(true)}><Upload size={16}/>导入 SKILL.md</Button><Button onClick={() => setEditor('new')}><Plus size={17}/>新建 Skill</Button></>}/>
    <ErrorNotice message={error}/><SuccessNotice message={success}/>
    {loading ? <Loader/> : !items.length ? <EmptyState icon={<Sparkles size={30}/>} title="还没有 Skill" description="导入或编写一份业务处理指引。"/> : <div className="resource-grid">{items.map(item => <article className="resource-card" key={item.id}><div className="resource-top"><Sparkles size={23}/><ScopeBadge scope={item.scope}/></div><h2>{item.name}</h2><p>{item.description || '暂无说明'}</p><div className="resource-meta"><span>v{item.revision}</span><span>{item.enabled ? '已启用' : '已停用'}</span></div><footer><Button variant="secondary" onClick={() => setEditor(item)}><BookOpen size={15}/>查看</Button><Button variant="ghost" onClick={() => setHistory(item)}>版本</Button>{canEdit(item, admin) && <button type="button" className="icon-button danger-text" aria-label={`删除 ${item.name}`} onClick={() => setDeleting(item)}><Trash2 size={16}/></button>}</footer></article>)}</div>}
    {history && <SkillHistory skill={history} onClose={() => setHistory(null)}/>}
    {editor && <SkillEditor value={editor === 'new' ? null : editor} admin={admin} onClose={() => setEditor(null)} onSaved={() => { setEditor(null); setSuccess('Skill 已保存。'); void load(); }}/ >}
    {importing && <SkillImport admin={admin} onClose={() => setImporting(false)} onSaved={() => { setImporting(false); setSuccess('Skill 已导入。'); void load(); }}/ >}
    {deleting && <Modal title="删除 Skill" onClose={() => { if (!busy) setDeleting(null); }}><div className="modal-body"><p>确认删除「{deleting.name}」？历史版本仍保留。</p></div><footer className="modal-footer"><Button variant="secondary" disabled={busy} onClick={() => setDeleting(null)}>取消</Button><Button variant="danger" busy={busy} onClick={async () => { setBusy(true); try { await remove(`/skills/${deleting.id}`); setDeleting(null); await load(); } catch (cause) { setError(errorText(cause)); setDeleting(null); } finally { setBusy(false); } }}>删除</Button></footer></Modal>}
  </div>;
}

export function SkillDocument({ value }: { value: Skill }) {
  const references = Object.entries(value.resources || {});
  return <article className="skill-document skill-detail" aria-label={`${value.name}详情`}>
    <header className="skill-detail-overview"><div className="skill-document-meta"><span className="skill-document-kind"><BookOpen size={14}/>Skill 指引</span><span className="skill-document-version">v{value.revision}</span><ScopeBadge scope={value.scope}/><span className={`skill-document-status${value.enabled ? ' enabled' : ''}`}><i aria-hidden="true"/>{value.enabled ? '已启用' : '已停用'}</span></div>
      {value.description && <p className="skill-document-description">{value.description}</p>}
    </header>
    <div className="skill-reading-surface"><MarkdownDocument text={value.instructions} label="流程指引"/></div>
    {(value.required_tools.length > 0 || references.length > 0) && <aside className="skill-supporting" aria-label="工具与参考资料">
      {value.required_tools.length > 0 && <details className="skill-support-section"><summary><span><Wrench size={16}/>所需工具<small>{value.required_tools.length}</small></span><ChevronDown size={16}/></summary><ul className="skill-tool-list">{value.required_tools.map(tool => <li key={tool}><code>{tool}</code></li>)}</ul></details>}
      {references.length > 0 && <section className="skill-references" aria-label="参考资料"><div className="skill-reference-heading"><FileText size={16}/><h3>参考资料</h3><span>{references.length}</span></div>{references.map(([name, text]) => <details className="skill-reference" key={name}><summary><span><FileText size={15}/><strong>{name}</strong></span><ChevronDown size={15}/></summary><div className="skill-reference-content"><MarkdownDocument text={text} label={name}/></div></details>)}</section>}
    </aside>}
  </article>;
}
export function SkillEditor({ value, admin, onClose, onSaved }: { value: Skill | null; admin: boolean; onClose: () => void; onSaved: () => void }) {
  const editable = !value || canEdit(value, admin), [editing, setEditing] = useState(!value);
  const [name, setName] = useState(value?.name || ''), [description, setDescription] = useState(value?.description || ''), [instructions, setInstructions] = useState(value?.instructions || '');
  const [scope, setScope] = useState<ResourceScope>(value?.scope || 'private'), [enabled, setEnabled] = useState(value?.enabled ?? true), [tools, setTools] = useState(value?.required_tools.join('\n') || ''), [resources, setResources] = useState(JSON.stringify(value?.resources || {}, null, 2));
  const [busy, setBusy] = useState(false), [error, setError] = useState('');
  return <Modal title={value ? value.name : '新建 Skill'} wide onClose={() => { if (!busy) onClose(); }}>
    {!editing && value ? <><div className="modal-body skill-detail-body"><SkillDocument value={value}/></div><footer className="modal-footer"><Button variant="secondary" onClick={onClose}>关闭</Button>{editable && <Button onClick={() => setEditing(true)}><Pencil size={15}/>编辑</Button>}</footer></> : <form onSubmit={async event => {
      event.preventDefault(); if (!editable || busy) return; setBusy(true); setError('');
      try { if (!name.trim() || !instructions.trim()) throw new Error('请填写名称和流程指引。'); const parsed = JSON.parse(resources); if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object' || Object.values(parsed).some(item => typeof item !== 'string')) throw new Error('参考资料须为名称与文本组成的 JSON 对象。');
        const body = { name: name.trim(), description, instructions, scope, enabled, required_tools: [...new Set(tools.split('\n').map(tool => tool.trim()).filter(Boolean))], resources: parsed };
        if (value) await patch(`/skills/${value.id}`, body); else await post('/skills', body); onSaved();
      } catch (cause) { setError(errorText(cause)); } finally { setBusy(false); }
    }}><div className="modal-body skill-editor-body"><ErrorNotice message={error}/><fieldset disabled={!editable || busy} className="plain-fieldset">
      <div className="two-col"><Field label="名称" required><input value={name} onChange={event => setName(event.target.value)} maxLength={100} required/></Field><ScopeField value={scope} onChange={setScope} admin={admin} disabled={!!value}/></div>
      <Field label="说明"><input value={description} onChange={event => setDescription(event.target.value)} maxLength={500}/></Field>
      <MarkdownDocument text={instructions} onChange={setInstructions} label="流程指引" disabled={busy} initialView="source"/>
      <details className="resource-advanced"><summary>工具与参考资料</summary><Field label="需要的工具" hint="每行一个工具名。"><textarea rows={3} value={tools} onChange={event => setTools(event.target.value)}/></Field><Field label="参考资料（名称与文本 JSON）"><textarea rows={5} value={resources} onChange={event => setResources(event.target.value)} spellCheck={false}/></Field></details>
      <label className="checkbox-row"><input type="checkbox" checked={enabled} onChange={event => setEnabled(event.target.checked)}/>启用 Skill</label>
    </fieldset></div><footer className="modal-footer"><Button variant="secondary" disabled={busy} onClick={onClose}>取消</Button><Button type="submit" busy={busy}>保存版本</Button></footer></form>}
  </Modal>;
}
function SkillImport({ admin, onClose, onSaved }: { admin: boolean; onClose: () => void; onSaved: () => void }) {
  const [markdown, setMarkdown] = useState(''), [scope, setScope] = useState<ResourceScope>('private'), [busy, setBusy] = useState(false), [error, setError] = useState('');
  return <Modal title="导入 SKILL.md" wide onClose={() => { if (!busy) onClose(); }}><form onSubmit={async event => { event.preventDefault(); if (busy || !markdown.trim()) return; setBusy(true); setError(''); try { await post('/skills/import', { markdown, scope }); onSaved(); } catch (cause) { setError(errorText(cause)); } finally { setBusy(false); } }}>
    <div className="modal-body"><ErrorNotice message={error}/><ScopeField value={scope} onChange={setScope} admin={admin} disabled={busy}/><Field label="选择 Markdown 文件"><input type="file" accept=".md,.markdown,text/markdown,text/plain" disabled={busy} onChange={async event => { const file = event.target.files?.[0]; event.target.value = ''; if (!file) return; setError(''); if (file.size > 200000) { setError('Skill 文件不得超过 200 KB。'); return; } setBusy(true); try { const text = await file.text(); if (text.length > 60000) throw new Error('Skill 内容不得超过 60000 字符。'); setMarkdown(text); } catch (cause) { setError(errorText(cause)); } finally { setBusy(false); } }}/></Field><MarkdownDocument text={markdown} onChange={setMarkdown} label="Skill 内容" disabled={busy} initialView="source"/></div>
    <footer className="modal-footer"><Button variant="secondary" disabled={busy} onClick={onClose}>取消</Button><Button type="submit" busy={busy} disabled={!markdown.trim()}>导入</Button></footer>
  </form></Modal>;
}
function SkillHistory({ skill, onClose }: { skill: Skill; onClose: () => void }) {
  const [versions, setVersions] = useState<Skill[]>([]), [selected, setSelected] = useState(skill.revision), [loading, setLoading] = useState(true), [error, setError] = useState('');
  useEffect(() => { let current = true; api<Skill[]>(`/skills/${skill.id}/versions`).then(result => { if (current) setVersions(result); }).catch(cause => { if (current) setError(errorText(cause)); }).finally(() => { if (current) setLoading(false); }); return () => { current = false; }; }, [skill.id]);
  const value = versions.find(version => version.revision === selected);
  return <Modal title={`${skill.name} · 历史版本`} wide onClose={onClose}><div className="modal-body skill-detail-body skill-history-body"><ErrorNotice message={error}/>{loading ? <Loader/> : <><Field label="版本"><select value={selected} onChange={event => setSelected(Number(event.target.value))}>{versions.map(version => <option key={version.revision} value={version.revision}>v{version.revision} · {version.name}</option>)}</select></Field>{value && <SkillDocument key={selected} value={value}/>}</>}</div><footer className="modal-footer"><Button variant="secondary" onClick={onClose}>关闭</Button></footer></Modal>;
}
