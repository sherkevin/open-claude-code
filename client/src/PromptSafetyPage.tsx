import { useCallback, useEffect, useRef, useState } from 'react';
import { Plus, RefreshCw, ShieldCheck, Trash2 } from 'lucide-react';
import { api, errorText, timeText } from './api';
import { Button, ErrorNotice, Field, Loader, PageHeader, SuccessNotice } from './components';
import './prompt-safety.css';

const endpoint = '/admin/prompt-safety';
const categoryLabels = {
  'Violent': '暴力',
  'Non-violent Illegal Acts': '非暴力违法行为',
  'Sexual Content or Sexual Acts': '性内容',
  'PII': '个人敏感信息',
  'Suicide & Self-Harm': '自杀与自伤',
  'Unethical Acts': '骚扰、仇恨等不当行为',
  'Copyright Violation': '版权侵害',
  'Jailbreak': '试图绕过模型指令',
};
const levelLabels = { Unsafe: '不安全', Controversial: '有争议' };
const modeLabels = { off: '审核关闭', rules: '仅主题规则', qwen: '主题规则与通用审核', invalid: '审核模式配置无效' };
const sourceLabels = { administrator: '后台保存的策略', file: '服务端 JSON 策略', unconfigured: '尚未保存策略' };
const savedMessages = { off: '策略已保存，审核开启后生效。', invalid: '策略已保存，请修正服务端审核模式。',
  rules: '策略已保存，后续提交将使用新规则。', qwen: '策略已保存，后续提交将使用新规则。' };
type Category = keyof typeof categoryLabels;
type Level = keyof typeof levelLabels;
type Rule = { category: string; enabled: boolean; terms: string[]; require_any: string[]; except_exact: string[]; message: string | null };
type EditableRule = Rule & { editorKey: number };
type Policy = { rules: Rule[]; blocked_message: string; qwen_block_levels: Level[]; qwen_block_categories: Category[] };
type Snapshot = { version: string; source: keyof typeof sourceLabels; mode: keyof typeof modeLabels; updated_at: string | null; policy: Policy };
type Draft = Omit<Policy, 'rules'> & { rules: EditableRule[] };

const trimList = (values: string[]) => values.map(value => value.trim()).filter(Boolean);
const toggle = <T,>(values: T[], value: T, checked: boolean) => checked ? [...values, value] : values.filter(item => item !== value);

function WordList({label, hint, words, onChange}: {label: string; hint: string; words: string[]; onChange: (words: string[]) => void}) {
  return <Field label={label} hint={hint}>
    <textarea rows={3} value={words.join('\n')} onChange={event => onChange(event.target.value.split('\n'))}/>
  </Field>;
}

export default function PromptSafetyPage() {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [saved, setSaved] = useState(false);
  const nextRuleKey = useRef(0);
  const apply = useCallback((value: Snapshot) => {
    setSnapshot(value);
    setDraft({...value.policy, rules: value.policy.rules.map(rule => ({...rule, editorKey: ++nextRuleKey.current}))});
  }, []);

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setSaved(false);
    setError('');
    try { apply(await api<Snapshot>(endpoint, {signal})); }
    catch (cause) { if (!signal?.aborted) setError(errorText(cause)); }
    finally { if (!signal?.aborted) setLoading(false); }
  }, [apply]);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const edit = (value: Partial<Draft>) => {
    setDraft(current => current && {...current, ...value});
    setSaved(false);
  };
  const editRule = (key: number, value: Partial<Rule>) => {
    setDraft(current => current && {...current, rules: current.rules.map(rule => rule.editorKey === key ? {...rule, ...value} : rule)});
    setSaved(false);
  };
  const addRule = () => {
    if (!draft) return;
    edit({rules: [...draft.rules, {editorKey: ++nextRuleKey.current, category: '', enabled: true, terms: [], require_any: [], except_exact: [], message: null}]});
  };

  const save = async () => {
    if (!snapshot || !draft || saving || loading) return;
    setError('');
    setSaved(false);
    const rules = draft.rules.map(({editorKey, ...rule}) => ({...rule, category: rule.category.trim(),
      terms: trimList(rule.terms), require_any: trimList(rule.require_any), except_exact: trimList(rule.except_exact),
      message: rule.message?.trim() || null}));
    const invalid = rules.findIndex(rule => !rule.terms.length || [rule.terms, rule.require_any, rule.except_exact]
      .some(list => list.length > 64 || list.some(word => word.length > 400)));
    if (invalid >= 0) {
      setError(`请检查第 ${invalid + 1} 条主题规则。至少填写一个匹配词，每组最多 64 项，每项最多 400 字。`);
      return;
    }
    if (!draft.blocked_message.trim() || !draft.qwen_block_levels.length || !draft.qwen_block_categories.length) {
      setError('请填写默认拒绝提示，并至少选择一个通用审核级别和一个类别。');
      return;
    }
    setSaving(true);
    try {
      const policy = {...draft, blocked_message: draft.blocked_message.trim(), rules};
      apply(await api<Snapshot>(endpoint, {method: 'PUT', body: JSON.stringify({version: snapshot.version, policy})}));
      setSaved(true);
    } catch (cause) { setError(errorText(cause)); }
    finally { setSaving(false); }
  };

  const busy = loading || saving;
  return <div className="page prompt-safety-page">
    <PageHeader eyebrow="工作空间管理" title="内容审核" description="管理用户消息进入模型前的审核规则。"
      actions={<Button variant="secondary" disabled={busy} onClick={() => void load()}><RefreshCw size={16}/>重新加载</Button>}/>
    <ErrorNotice message={error}/>
    <SuccessNotice message={saved && snapshot ? savedMessages[snapshot.mode] : ''}/>
    {loading ? <Loader text="正在读取审核策略…"/> : snapshot && draft && <>
      <div className="info-strip"><ShieldCheck size={20}/><p><strong>{modeLabels[snapshot.mode]}</strong> · {sourceLabels[snapshot.source]}{snapshot.updated_at && ` · ${timeText(snapshot.updated_at)} 更新`}。审核模式由部署配置控制。</p></div>
      <form onSubmit={event => {event.preventDefault(); void save();}}>
        <fieldset className="prompt-safety-fields" disabled={busy}>
          <section className="prompt-safety-card">
            <h2>通用审核与默认提示</h2>
            <Field label="默认拒绝提示" required><input value={draft.blocked_message} required maxLength={160} onChange={event => edit({blocked_message: event.target.value})}/></Field>
            <fieldset className="prompt-safety-options"><legend>拦截级别</legend>
              {(Object.keys(levelLabels) as Level[]).map(level => <label key={level}><input type="checkbox" checked={draft.qwen_block_levels.includes(level)} onChange={event => edit({qwen_block_levels: toggle(draft.qwen_block_levels, level, event.target.checked)})}/>{levelLabels[level]}</label>)}
            </fieldset>
            <fieldset className="prompt-safety-options"><legend>拦截类别</legend>
              {(Object.keys(categoryLabels) as Category[]).map(category => <label key={category}><input type="checkbox" checked={draft.qwen_block_categories.includes(category)} onChange={event => edit({qwen_block_categories: toggle(draft.qwen_block_categories, category, event.target.checked)})}/>{categoryLabels[category]}</label>)}
            </fieldset>
          </section>
          <section className="prompt-safety-card">
            <header><h2>主题规则</h2><Button variant="secondary" disabled={draft.rules.length >= 100} onClick={addRule}><Plus size={16}/>添加规则</Button></header>
            <p>首条命中的规则决定提示。整句例外只对所属规则有效。</p>
            {draft.rules.length === 0 && <p>当前没有主题规则。</p>}
            {draft.rules.map((rule, index) => <fieldset className="prompt-safety-rule" key={rule.editorKey}>
              <legend>{rule.category || '新规则'}</legend>
              <div className="prompt-safety-rule-heading">
                <label className="prompt-safety-toggle"><input type="checkbox" checked={rule.enabled} onChange={event => editRule(rule.editorKey, {enabled: event.target.checked})}/>启用规则</label>
                <Button variant="ghost" aria-label={`移除第 ${index + 1} 条规则`} onClick={() => edit({rules: draft.rules.filter(item => item.editorKey !== rule.editorKey)})}><Trash2 size={16}/>移除</Button>
              </div>
              <Field label="主题标识" hint="以小写字母开头，可使用小写字母、数字、下划线或连字符。" required>
                <input required maxLength={60} pattern={'[a-z][a-z0-9_\\-]{0,59}'} value={rule.category} onChange={event => editRule(rule.editorKey, {category: event.target.value})}/>
              </Field>
              <div className="prompt-safety-words">
                <WordList label="匹配词" hint="每行一项，命中任一项。" words={rule.terms} onChange={terms => editRule(rule.editorKey, {terms})}/>
                <WordList label="上下文词" hint="选填。有配置时还须命中其中一项。" words={rule.require_any} onChange={require_any => editRule(rule.editorKey, {require_any})}/>
                <WordList label="整句例外" hint="选填。与完整消息匹配时跳过该规则。" words={rule.except_exact} onChange={except_exact => editRule(rule.editorKey, {except_exact})}/>
              </div>
              <Field label="规则拒绝提示" hint="留空时使用默认提示。"><input value={rule.message || ''} maxLength={160} onChange={event => editRule(rule.editorKey, {message: event.target.value})}/></Field>
            </fieldset>)}
          </section>
        </fieldset>
        <Button type="submit" busy={saving}>保存策略</Button>
      </form>
    </>}
  </div>;
}
