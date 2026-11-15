import type { Conversation } from './types';

export default function RuntimeBadge({ runtime }: { runtime: Conversation['runtime_type'] }) {
  const label = runtime === 'harness' ? '标准引擎'
    : runtime === 'claude' ? '兼容引擎' : '执行引擎待确认';
  const description = runtime === 'harness'
    ? '本会话使用标准引擎，回答使用你选择的模型。'
    : runtime === 'claude'
      ? '本会话使用兼容引擎并保留原有历史，回答仍使用你选择的模型。'
      : '服务端尚未提供本会话的执行引擎，不能从模型名称推断。';
  return <span className="badge" title={description} aria-label={`本会话执行引擎：${label}`}>{label}</span>;
}
