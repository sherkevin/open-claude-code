import { createContext, useContext, useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { api } from './api';
import { Button, ErrorNotice, Field, Modal } from './components';

export interface SingleChildDraft {
  runId: string;
  text: string;
}

interface FollowupComposer {
  text: string;
  blocked: string;
  hasSelections: boolean;
  canDelegate: boolean;
  availableRunId?: string;
  transfer: (runId: string, text: string) => boolean;
  write: (expected: string, next: string) => boolean;
}

export const FailedChildComposer = createContext<FollowupComposer | null>(null);

export function childFollowupDraft(previous: string, title: string, requirement: string) {
  if (!requirement.trim()) return null;
  const request = `本轮请直接回答「${title}」这一项。\n${requirement.trim()}`;
  const next = previous ? `${previous}\n\n${request}` : request;
  return next.length <= 50000 ? next : null;
}

function FollowupDialog({ title, parentRunId, composer, close }: { title: string; parentRunId?: string; composer: FollowupComposer; close: () => void }) {
  const [requirement, setRequirement] = useState('');
  const [error, setError] = useState('');
  const [target] = useState(() => parentRunId && composer.availableRunId !== parentRunId ? composer.availableRunId : undefined);
  const targetActive = !!target && target === composer.availableRunId;
  const [available, setAvailable] = useState<boolean | null>(null);
  useEffect(() => {
    if (!targetActive || !target) return;
    const controller = new AbortController();
    void api<{ plan_id: string; children: unknown[] }>(`/runs/${encodeURIComponent(target)}/subagents`, { signal: controller.signal }).then(plan => {
      if (!controller.signal.aborted) setAvailable(plan.plan_id === target && Array.isArray(plan.children) && !plan.children.length);
    }, () => { if (!controller.signal.aborted) setAvailable(false); });
    return () => controller.abort();
  }, [target, targetActive]);
  const next = childFollowupDraft(composer.text, title, requirement);
  function prepare() {
    if (composer.blocked || next === null) return;
    if (composer.write(composer.text, next)) close();
    else setError('会话或草稿已变化，请核对后再写入。');
  }
  return <Modal title="就此项另问" description={`失败子项 · ${title}`} onClose={close}>
    <div className="modal-body">
      <p>原委派的逐字说明未保存。请写明要补做的内容，核对草稿后点「发送消息」，由父助手在新一轮回答。</p>
      <Field label="本次要求"><textarea autoFocus rows={4} value={requirement} onChange={event => setRequirement(event.target.value)}/></Field>
      {composer.canDelegate && <p>如需一个独立子任务，请在支持分派的新一轮运行时打开「会话侧栏 → 子任务 → 分配子任务」，填写一项要求。</p>}
      {targetActive && <p role="status">{available === null ? '正在核对本轮分配入口…' : available
        ? '本轮尚无子计划。可把本次要求带到分配框，点「启动子任务」才会提交一项。'
        : '当前未确认有可用的单项分配入口。'}</p>}
      {target && !targetActive && <p>刚才的分配轮次已结束，本次要求仍在这里。</p>}
      {composer.text && <p>普通输入框已有草稿。追加会保留原文，将这项要求放在末尾。</p>}
      {composer.hasSelections && <p>普通输入框已选附件或技能，请在发送前核对。</p>}
      <ErrorNotice message={composer.blocked || error || (requirement.trim() && next === null ? '合并后的草稿超过 50000 字。' : '')}/>
    </div>
    <footer className="modal-footer">
      <Button variant="secondary" onClick={close}>取消</Button>
      {target && targetActive && available && <Button variant="secondary" disabled={!requirement.trim() || requirement.trim().length > 2000} onClick={() => {
        if (composer.transfer(target, requirement.trim())) close();
        else setError('本轮已变化或分配框已有待处理的单项草稿，本次要求仍在这里。');
      }}>带到本轮分配框</Button>}
      <Button disabled={!!composer.blocked || next === null} onClick={prepare}>{composer.text ? '追加到现有草稿' : '写入提问草稿'}</Button>
    </footer>
  </Modal>;
}

export function FailedChildFollowup({ title, parentRunId }: { title: string; parentRunId?: string }) {
  const composer = useContext(FailedChildComposer);
  const [open, setOpen] = useState(false);
  if (!composer) return null;
  return <div>
    <Button variant="secondary" onClick={() => setOpen(true)}>就此项另问</Button>
    {open && createPortal(<FollowupDialog title={title} parentRunId={parentRunId} composer={composer} close={() => setOpen(false)}/>, document.body)}
  </div>;
}
