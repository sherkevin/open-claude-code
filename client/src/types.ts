export interface AppConfig { brand_name: string; upload_max_bytes: number; auth_types: string[]; model_label: string; default_model_id?: string; default_reasoning_level?: string }
export interface ModelOption { id: string; label: string; available: boolean; unavailable_reason?: string; capabilities?: string[]; reasoning_levels: Array<{ id: string; label: string }>; default_reasoning_level: string; requires_new_conversation_on_switch?: boolean }
export interface ModelRecommendation { model_id: string; model_label: string; reasoning_level: string; reason: string; applied: false; router: { model_id: string; mode: string; input_chars: number; candidate_count: number; max_output_chars: number; estimated_cost_usd: number | null } }
export interface ModelSelection { model_id: string; reasoning_level: string }
export interface User { id: string; username: string; display_name: string; role: 'admin' | 'member'; enabled: boolean }
export interface MCPTool { name: string; description: string; inputSchema: Record<string, unknown> }
export interface ScopedResource { owner_id?: string | null; scope?: 'private' | 'team'; can_edit?: boolean }
export interface Connection extends ScopedResource {
  id: string; name: string; transport: 'http'; url: string; auth_type: 'none' | 'bearer';
  credential_present: boolean; enabled: boolean; status: 'untested' | 'connected' | 'needs_auth' | 'failed';
  source_metadata?: Record<string,string>; tools: MCPTool[]; last_error: string | null; last_tested_at: string | null; revision: number;
}
export interface MCPImportPreview {
  format: 'openai_mcp' | 'mcp_servers';
  connections: Array<{
    name: string; url: string; transport: 'http'; auth_type: 'none' | 'bearer'; credential_present: boolean;
    allowed_tools: null | string[] | { tool_names?: string[]; read_only?: boolean };
    require_approval: 'always' | 'never' | Record<string, unknown>;
  }>;
  warnings: string[];
}
export interface MCPImportResult { connections: Connection[]; warnings: string[]; binding_required: true }
export interface MemoryIndex {
  conversation_id: string; assistant_id: string; scope: 'conversation'; revision: number; busy: boolean;
  files: Array<{ path: string; size: number; updated_at: string }>;
  max_file_bytes: number; max_total_bytes: number;
}
export interface MemoryFile { path: string; content: string; revision: number; updated_at: string }
export interface AssistantBinding { connection_id: string; allowed_tools: string[]; auto_approved_tools: string[] }
export interface Assistant extends ScopedResource {
  id: string; name: string; description: string; instructions: string; enabled: boolean;
  access: { all_members: boolean; user_ids: string[] }; connections: AssistantBinding[]; revision: number;
  knowledge_base_ids?: string[]; skill_ids?: string[]; web_search_enabled?: boolean; default_model_id?: string | null; default_reasoning_level?: string | null;
}
export interface Conversation { id: string; assistant_id: string; title: string; archived: boolean; active_run_id: string | null; created_at: string; updated_at: string; model_id: string; model_label: string; reasoning_level: string; auto_model_routing?: boolean; has_native_session?: boolean; category?: string; tags?: string[] }
export interface Message { id: string; conversation_id: string; role: 'user' | 'assistant'; text: string; file_ids: string[]; run_id: string | null; created_at: string; citations?: EvidenceSource[]; skills?: Array<{id:string;name:string;revision:number}> }
export interface Skill extends ScopedResource { id: string; name: string; description: string; instructions: string; enabled: boolean; revision: number; required_tools: string[]; resources?: Record<string,string> }
export interface KnowledgeBase extends ScopedResource { id:string; name:string; description:string; enabled:boolean; default_for_new_assistants:boolean; document_count:number; index_status?:string }
export interface KnowledgeDocument { id:string; kb_id:string; name:string; mime_type:string; version:number; sha256:string; status:string; error?:string|null; page_count:number; chunk_count:number; created_at:string }
export interface KnowledgeHit { document_id:string; name?:string; title?:string; text?:string; excerpt?:string; page?:number; section?:string; version?:number; score?:number }
export interface KnowledgeGraph { versioned:boolean; source:string; query:string; nodes:Array<{id:string;label:string;kind:string;source:{document_id:string;version:number;page:number|null;paragraph:number|null;start:number|null;end:number|null}}>; edges:Array<{id:string;source:string;target:string;relation:string;provenance:{document_id:string;version:number;page:number|null;paragraph:number|null;start:number|null;end:number|null}}> }
export interface EvidenceSource { id:string; number?:number; kind:'data'|'knowledge'|'web'; title:string; locator:Record<string,unknown>; excerpt:string; created_at:string; original_url?:string; document_id?:string; document_version?:number; content_url?:string }
export interface RunTimings { runtime_ready_ms?:number; first_model_event_ms?:number; first_text_ms?:number; total_ms?:number }
export interface StoredFile { id: string; name: string; mime_type: string; size: number; created_at: string }
export interface Approval { id: string; run_id: string; tool_name: string; arguments: Record<string, unknown>; status: string; created_at: string; expires_at: string; risk_level?: 'low' | 'medium' | 'high'; approval_policy?: string; impact?: string; argument_digest?: string; decided_at?: string | null; decided_by?: string | null; decision_source?: string | null; strategy?: 'user' | 'model' | 'dual'; approval_mode?: 'single' | 'dual'; required_approvals?: number; approver_ids?: string[]; approval_policy_id?: string | null; approval_policy_revision?: number | null; approval_decision_reason?: string | null }
export interface ModelApprovalPolicy { assistant_id: string; enabled: boolean; reviewer_mode: 'synthetic'; model_id: string; allowed_tools: string[]; allowed_data_scopes: string[]; max_amount: number; allowed_operations: string[]; revision: number; updated_at?: string | null; updated_by?: string | null }
export interface HighRiskApprovalPolicy { assistant_id: string; enabled: boolean; policy_id: string; allowed_tools: string[]; admin_allowlist: string[]; required_approvals: 2; revision: number; updated_at?: string | null; updated_by?: string | null }
export type RunStatus = 'queued' | 'running' | 'awaiting_approval' | 'cancelling' | 'succeeded' | 'failed' | 'cancelled' | 'interrupted';
export interface NativeContext {
  source: 'native_runtime'; status: 'available' | 'unavailable' | 'not_measured';
  total_tokens: number | null; max_tokens: number | null; percentage: number | null;
  auto_compact_enabled: boolean | null; auto_compact_threshold: number | null;
  model: string | null; measured_at: string | null; cached: boolean; error: string | null;
  last_compaction?: { run_id: string; status: CompactionStatus; trigger: string | null; pre_tokens: number | null; created_at: string; error?: string | null; followup_status?: string | null } | null;
}
export type CompactionStatus = 'started' | 'completed' | 'not_needed' | 'failed' | 'cancelled' | 'interrupted' | 'unknown';
export interface CompactionInfo { status: CompactionStatus; trigger: string | null; pre_tokens: number | null; before: NativeContext | null; after: NativeContext | null; error?: string | null; followup_status?: string | null }
export interface Run { id: string; conversation_id: string; status: RunStatus; execution_kind?: 'product_metadata' | 'model'; identity_source?: 'run_selection' | null; model_call_count?: number | null; kind?: 'chat' | 'compact'; compaction?: CompactionInfo | null; assistant_message_id: string | null; error: string | null; usage: Record<string, unknown> | null; approvals: Approval[]; created_at: string; completed_at: string | null; timings?:RunTimings }
export interface UsageSummary { total_runs: number; successful_runs: number; input_tokens: number; output_tokens: number; estimated_cost_usd: number | string | null; currency: string; users: Array<Record<string, unknown>> }
export interface StreamEvent { id: number; run_id: string; type: string; created_at: string; data: Record<string, unknown> }
export const isActive = (status: RunStatus) => ['queued', 'running', 'awaiting_approval', 'cancelling'].includes(status);
export const statusLabels: Record<RunStatus, string> = { queued: '等待执行', running: '正在执行', awaiting_approval: '等待你的确认', cancelling: '正在停止', succeeded: '已完成', failed: '执行失败', cancelled: '已停止', interrupted: '执行已中断' };
