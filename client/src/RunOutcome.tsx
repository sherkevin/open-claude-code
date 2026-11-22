import { Pencil, RefreshCw } from 'lucide-react';
import { Button } from './components';
import { toolLabel } from './display';
import { statusLabels, type Run } from './types';

export default function RunOutcome({ run, busy, disabled, onRetry, onAdjust, onContext }: {
  run: Run; busy: boolean; disabled: boolean; onRetry: () => void; onAdjust: () => void; onContext: () => void;
}) {
  const recovery = run.recovery;
  const answerOnly = recovery?.mode === 'answer_only';
  const restart = recovery?.interrupted_from;
  return <div className="run-outcome">
    <span className={`badge ${run.status === 'failed' ? 'amber' : ''}`}>{statusLabels[run.status]}</span>
    <p>{run.error || '本次任务未完成。请核对已记录的执行状态。'}</p>
    {restart && <p>重启前状态为「{statusLabels[restart]}」。{restart === 'awaiting_approval' && '原审批已结束。恢复时需重新审批尚未执行的操作。'}</p>}
    {Boolean(recovery?.operations.length) && <ul aria-label="上一轮操作状态">
      {recovery?.operations.map((operation, index) => <li key={`${operation.tool_name}-${index}`}>
        {toolLabel(operation.tool_name)} · {operation.state === 'completed' ? '已记录成功结果' : '执行结果待核对'}
      </li>)}
      {recovery?.operations_truncated && <li>其余操作可在内容轨迹中核对。</li>}
    </ul>}
    <div className="run-outcome-actions">
      {run.kind === 'compact' ? <Button variant="secondary" onClick={onContext}>查看上下文</Button> :
        run.error_code === 'MCP_RESULT_TOO_LARGE' && !answerOnly ? <>
          <Button variant="secondary" onClick={onAdjust}><Pencil size={15}/>调整查询条件</Button>
          <small>缩小查询范围后再发起任务。</small>
        </> : <>
          <Button variant="secondary" busy={busy} disabled={disabled} onClick={onRetry}><RefreshCw size={15}/>
            {answerOnly ? '继续整理结果' : restart ? '恢复任务' : '重试本次任务'}
          </Button>
          <small>{answerOnly ? '根据已记录的结果继续回答，工具保持禁用。' : '使用原问题、附件和 Skill 恢复任务，待执行操作仍按当前策略审批。'}</small>
        </>}
    </div>
  </div>;
}
