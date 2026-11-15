import type { Conversation } from './types';

export function groupConversations(items: Conversation[]) {
  const groups = new Map<string, Conversation[]>();
  for (const item of items) {
    const project = item.category?.trim() || '';
    groups.set(project, [...(groups.get(project) || []), item]);
  }
  return [...groups].map(([name, conversations]) => ({ name, conversations }));
}
