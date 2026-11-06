import { Sparkles, X } from 'lucide-react';
import type { Skill } from './types';

export function mentionQuery(text:string,caret:number) {const match=text.slice(0,caret).match(/(?:^|\s)@([^\s@]*)$/);return match?{query:match[1],start:caret-match[1].length-1,end:caret}:null;}
/**
 * A bounded, local intent completion. It is deliberately a suggestion that
 * Tab accepts; it never sends text or calls a model while the user is typing.
 */
export function intentCompletion(text:string): string | null {
  const value = text.trim();
  if (!value || /[。！？?!；;]$/.test(value)) return null;
  const suggestions: Array<[RegExp, string]> = [
    [/^(分析|评估|判断)/, '，列出关键依据、风险与可执行建议。'],
    [/^(查询|查找|统计)/, '，说明数据来源、时间范围、筛选条件和异常项。'],
    [/^(比较|对比)/, '，按适配性、成本、收益与风险给出结论。'],
    [/^(排查|诊断|解决)/, '，按现象、证据、可能原因、验证步骤和修复建议输出。'],
    [/^(总结|整理)/, '，提炼关键结论、依据和下一步行动。'],
    [/^(生成|制定|设计)/, '，先明确目标和约束，再给出可执行方案与验收标准。'],
  ];
  const match = suggestions.find(([pattern]) => pattern.test(value));
  return value + (match ? match[1] : '，先理解目标和约束，再给出依据充分、可执行的结果。');
}

export function selectedSkillIds(selected:string[],skills:Skill[]) {return [...new Set(selected)].filter(id=>skills.some(s=>s.id===id&&s.enabled)).slice(0,5);}
export function SkillChips({selected,skills,onRemove,disabled}:{selected:string[];skills:Skill[];onRemove:(id:string)=>void;disabled:boolean}) {return <div className="skill-chips">{selected.map(id=>{const skill=skills.find(s=>s.id===id);return skill?<span className="skill-chip" key={id}><Sparkles size={13}/>@{skill.name}<small>v{skill.revision}</small><button type="button" disabled={disabled} aria-label={`移除 Skill ${skill.name}`} onClick={()=>onRemove(id)}><X size={12}/></button></span>:null;})}</div>;}
export default function SkillPicker({skills,selected,query,onSelect,onClose,highlighted=0}:{highlighted?:number;skills:Skill[];selected:string[];query:string;onSelect:(skill:Skill)=>void;onClose:()=>void}) {const matches=skills.filter(s=>s.enabled&&!selected.includes(s.id)&&(s.name+' '+s.description).toLowerCase().includes(query.toLowerCase()));return <div className="skill-picker" role="dialog" aria-label="选择本次 Skill"><div><strong><Sparkles size={14}/>选择本次 Skill</strong><button type="button" className="icon-button" aria-label="关闭 Skill 选择" onClick={onClose}><X size={14}/></button></div>{matches.length?matches.map((skill,index)=><button type="button" key={skill.id} className={index===highlighted?'highlighted':''} disabled={selected.length>=5} onClick={()=>onSelect(skill)}><span><strong>@{skill.name}</strong><small>{skill.description}</small></span><small>v{skill.revision}</small></button>):<p>当前助手没有匹配的 Skill。可在助手设置中绑定。</p>}</div>;}
