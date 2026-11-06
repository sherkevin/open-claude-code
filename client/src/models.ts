import { patch } from './api';
import type { AppConfig, Conversation, ModelOption, ModelSelection } from './types';

export function modelSelection(models: ModelOption[], config: Pick<AppConfig, 'default_model_id' | 'default_reasoning_level'>, previous?: ModelSelection): ModelSelection {
  const preferred = models.find(model => model.id === previous?.model_id);
  const model = preferred || models.find(model => model.id === config.default_model_id) || models.find(model => model.available) || models[0];
  if (!model) return { model_id: '', reasoning_level: '' };
  const requestedLevel = preferred ? previous?.reasoning_level : config.default_reasoning_level;
  return { model_id: model.id, reasoning_level: model.reasoning_levels.some(level => level.id === requestedLevel) ? requestedLevel! : model.default_reasoning_level };
}

/** Only known public catalog identities can become labels; never echo an unknown routing ID. */
export function publicModelLabel(models: ModelOption[], id: string | null | undefined): string {
  return models.find(model => model.id === id || model.label === id)?.label || '模型信息暂不可用';
}

/** Preserve existing context when either route cannot continue another model's history. */
export function modelSwitchRequiresNewConversation(models: ModelOption[], currentId: string, nextId: string, hasHistory: boolean): boolean {
  if (!hasHistory || currentId === nextId) return false;
  return models.some(model => (model.id === currentId || model.id === nextId) && model.requires_new_conversation_on_switch === true);
}

export function conversationHasModelHistory(hasNativeSession: boolean | undefined, messageCount: number): boolean {
  return hasNativeSession ?? messageCount > 0;
}

/** Explicit reconnection must PATCH even when the public selection is unchanged. */
export async function saveModelSelection(conversationId: string, current: ModelSelection, next: ModelSelection, reconnect = false): Promise<Conversation | null> {
  if (!reconnect && next.model_id === current.model_id && next.reasoning_level === current.reasoning_level) return null;
  return patch<Conversation>(`/conversations/${encodeURIComponent(conversationId)}`, next);
}
