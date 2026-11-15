import { api } from './api';
import type { AppConfig } from './types';

export type ProviderId = 'bing_rss' | 'searxng' | 'brave' | 'deepseek_api' | 'public_http' | 'deepseek_webtools' | 'brave_images';
export type CapabilityKind = 'search' | 'fetch' | 'ocr' | 'image_search';
export type EgressMode = 'deny' | 'approval' | 'allow';
export type VerificationState = 'not_tested' | 'verified' | 'failed';
export interface DomainPolicy { mode: 'public' | 'allowlist'; hosts: string[] }
export interface EgressPolicy { query: EgressMode; url: EgressMode; upload: EgressMode }
export interface CapabilityLimits { calls_per_run: number; calls_per_day: number; max_upload_bytes: number }
export interface CapabilityRoute {
  enabled: boolean; preferred: ProviderId; fallbacks: ProviderId[]; timeout_seconds: number; max_attempts: number;
  domains: DomainPolicy; egress: EgressPolicy; limits: CapabilityLimits;
  mode_providers?: Partial<Record<'text_search' | 'reverse_image', ProviderId>>;
}
export interface ProviderView {
  id: ProviderId; configured: boolean; credential_present: boolean; verification: VerificationState;
  verification_checks: Record<string, VerificationState>; capabilities: CapabilityKind[];
  modes: Record<string, string[]>; revision: number;
}
export interface CredentialState { present: boolean; revision: number; verified: boolean }
export interface CapabilityConfiguration {
  revision: number; origin: 'environment' | 'administrator'; updated_at: string | null; schema_version: 2;
  search: CapabilityRoute; fetch: CapabilityRoute; ocr: CapabilityRoute; image_search: CapabilityRoute;
  providers: ProviderView[]; credentials: Record<ProviderId, CredentialState>;
}
export interface DiagnosticResult {
  capability: CapabilityKind; status: 'verified' | 'failed' | 'not_configured' | 'unsupported';
  error_code: string | null; latency_ms: number | null; sample_id: string; checked_at: string;
}
export interface DiagnosticResponse { provider_id: ProviderId; provider_revision: number; settings_revision: number; results: DiagnosticResult[] }

export const providerLabels: Record<ProviderId, string> = {
  bing_rss: 'Bing RSS', searxng: 'SearXNG', brave: 'Brave', deepseek_api: 'DeepSeek 官方扩展服务',
  public_http: '工作台网页读取器', deepseek_webtools: 'DeepSeek 图片工具', brave_images: 'Brave 图片搜索',
};
export const capabilityLabels: Record<CapabilityKind, string> = {
  search: '网页搜索', fetch: '网页读取', ocr: '文档识别', image_search: '图片搜索',
};
export const canManageCapabilities = (admin: boolean, config: Pick<AppConfig, 'deployment_mode'>) => admin && config.deployment_mode !== 'trial';
export const loadCapabilitySettings = () => api<CapabilityConfiguration>('/capability-settings');
export const saveCapabilitySettings = (value: CapabilityConfiguration) => api<CapabilityConfiguration>('/capability-settings', {
  method: 'PUT', body: JSON.stringify({
    revision: value.revision, search: value.search, fetch: value.fetch, ocr: value.ocr, image_search: value.image_search,
  }),
});
export const saveProviderCredential = (provider: ProviderId, revision: number, credential: string) =>
  api<CredentialState>(`/capability-settings/providers/${encodeURIComponent(provider)}/credential`, {
    method: 'PUT', body: JSON.stringify({revision, credential}),
  });
export const deleteProviderCredential = (provider: ProviderId, revision: number) =>
  api<CredentialState>(`/capability-settings/providers/${encodeURIComponent(provider)}/credential`, {
    method: 'DELETE', body: JSON.stringify({revision}),
  });
export const runProviderDiagnostics = (provider: ProviderId) =>
  api<DiagnosticResponse>(`/capability-settings/providers/${encodeURIComponent(provider)}/diagnostics`, {method: 'POST'});

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
