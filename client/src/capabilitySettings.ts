import { api } from './api';
import type { AppConfig } from './types';

export type ProviderId = 'bing_rss' | 'public_http' | 'searxng' | 'brave' | 'deepseek_api';
export type CapabilityKind = 'search' | 'fetch';
export interface CapabilityRoute { enabled: boolean; preferred: ProviderId; fallbacks: ProviderId[]; timeout_seconds: number }
export interface CapabilityConfiguration {
  revision: number; origin: 'environment' | 'administrator'; updated_at: string | null;
  search: CapabilityRoute; fetch: CapabilityRoute;
  providers: {id: ProviderId; configured: boolean; verification: 'not_tested'; capabilities: CapabilityKind[]}[];
  ocr: {status: 'not_integrated'; configurable: false; knowledge_fallback_configured: boolean};
  image_search: {status: 'not_integrated'; configurable: false};
}
export const providerLabels: Record<ProviderId, string> = {
  bing_rss: 'Bing RSS', public_http: '工作台网页读取器', searxng: 'SearXNG', brave: 'Brave', deepseek_api: 'DeepSeek 官方扩展服务',
};
export const canManageCapabilities = (admin: boolean, config: Pick<AppConfig, 'deployment_mode'>) => admin && config.deployment_mode !== 'trial';
export const loadCapabilitySettings = () => api<CapabilityConfiguration>('/capability-settings');
export const saveCapabilitySettings = (value: CapabilityConfiguration) => api<CapabilityConfiguration>('/capability-settings', {
  method: 'PUT', body: JSON.stringify({revision: value.revision, search: value.search, fetch: value.fetch}),
});

export interface CapabilityPageState { value: CapabilityConfiguration | null; phase: 'loading' | 'ready' | 'saving' | 'error'; error: string; saved: boolean }
export type CapabilityPageAction = {type: 'load' | 'save'} | {type: 'loaded' | 'saved' | 'edit'; value: CapabilityConfiguration} | {type: 'error'; error: string};
export const capabilityInitialState: CapabilityPageState = {value: null, phase: 'loading', error: '', saved: false};
export function capabilityPageReducer(state: CapabilityPageState, action: CapabilityPageAction): CapabilityPageState {
  if (action.type === 'load') return {...state, phase: 'loading', error: '', saved: false};
  if (action.type === 'save') return {...state, phase: 'saving', error: '', saved: false};
  if (action.type === 'error') return {...state, phase: 'error', error: action.error, saved: false};
  if ('value' in action) return {value: action.value, phase: 'ready', error: '', saved: action.type === 'saved'};
  return state;
}
