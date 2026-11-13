import type { Conversation } from './types';

export default function RuntimeBadge({ runtime }: { runtime: Conversation['runtime_type'] }) {
  const label = runtime === 'harness' ? 'DeepSeek Harness'
    : runtime === 'claude' ? 'Claude Agent SDK' : '执行框架待确认';
  const description = runtime === 'harness'
    ? '本会话使用 DeepSeek Harness。完整功能仍在逐项迁移验收；执行框架与回答模型是不同配置。'
    : runtime === 'claude'
      ? '本会话保留 Claude Agent SDK 和原有历史；新会话默认值不会迁移本会话，回答仍使用你选择的模型。'
      : '服务端尚未提供本会话的执行框架，不能从模型名称推断。';
  return <span className="badge" title={description} aria-label={`本会话执行框架：${label}`}>{label}</span>;
}
