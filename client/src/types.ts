export interface AppConfig {
  brand_name: string; upload_max_bytes: number; auth_types: string[]; model_label: string;
  default_model_id?: string; default_reasoning_level?: string;
  deployment_mode?: 'normal' | 'trial'; capabilities?: string[]; allowed_retrieval_modes?: KnowledgeRetrievalMode[];
  feishu_h5?: { enabled: boolean; app_id: string; path: string };
}

/** Normal deployments keep their existing UI; trial deployments only expose advertised capabilities. */
export const configAllows = (config: AppConfig, capability: string) =>
  config.deployment_mode !== 'trial' || Boolean(config.capabilities?.includes(capability));
export interface ModelOption { id: string; label: string; available: boolean; unavailable_reason?: string; capabilities?: string[]; reasoning_levels: Array<{ id: string; label: string }>; default_reasoning_level: string; requires_new_conversation_on_switch?: boolean }
export interface ProviderPreflight {
  schema_version: string;
  router: {
    fitness: { target_model_id?: string | null; target_available?: boolean; provider_configured?: boolean; status?: string };
    tab: { target_model_id?: string | null; target_available?: boolean; provider_configured?: boolean; status?: string };
  };
  roles?: Record<string, { status?: string; provider_configured?: boolean; feature_enabled?: boolean; call_enabled?: boolean }>;
  provider_attempted: boolean;
  evidence_boundary?: string;
}
export interface ModelRouteEvidence { schema_version: string; policy_version: string; decision_mode: string; input_digest: string; candidate_model_ids: string[]; target_model_id: string; target_status: string; selected_model_id?: string; fallback_reason?: string; provider_attempted?: boolean }
export interface ModelRecommendation { model_id: string; model_label: string; reasoning_level: string; reason: string; applied: false; router: { model_id: string; mode: string; input_chars: number; candidate_count: number; max_output_chars: number; estimated_cost_usd: number | null; target_model_id?: string; target_available?: boolean; target_status?: string; target_error?: string; provider_configured?: boolean; provider_status?: string; provider_attempted?: boolean; rejection_reason?: string; evidence?: ModelRouteEvidence } }
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
export interface MemoryMutationResult { native_epoch?: number; context_reset?: boolean; operation_id?: string; mutation_status?: string }
export interface MemoryIndex extends MemoryMutationResult {
  conversation_id: string; assistant_id: string; scope: 'conversation'; revision: number; busy: boolean;
  files: Array<{ path: string; size: number; updated_at: string }>;
  max_file_bytes: number; max_total_bytes: number;
}
export interface MemoryFile extends MemoryMutationResult { path: string; content: string; revision: number; updated_at: string }
export interface MemoryApprovalContent { conversation_id: string; old_content_sha256: string|null; content_digest: string; path: string; content: string; expected_revision: number; native_epoch: number; operation_id: string }
export interface AssistantBinding { connection_id: string; allowed_tools: string[]; auto_approved_tools: string[]; argument_constraints?: Record<string, Record<string, unknown>> }
export interface Assistant extends ScopedResource {
  id: string; name: string; description: string; instructions: string; enabled: boolean;
  access: { all_members: boolean; user_ids: string[] }; connections: AssistantBinding[]; revision: number;
  knowledge_base_ids?: string[]; skill_ids?: string[]; web_search_enabled?: boolean; default_model_id?: string | null; default_reasoning_level?: string | null;
}
export type WorkflowNodeType = 'input' | 'agent' | 'skill' | 'knowledge_search' | 'mcp_read' | 'condition' | 'approval' | 'artifact';
export interface WorkflowNode { id: string; type: WorkflowNodeType; label: string; config: Record<string, unknown> }
export interface WorkflowEdge { source: string; target: string; label: string }
export interface WorkflowGraph { nodes: WorkflowNode[]; edges: WorkflowEdge[] }
export interface WorkflowCanvasReferenceHighlight { sourceNodeId: string; targetNodeId: string; edgeKeys: string[] }
export interface WorkflowCanvasNodeDiagnostic { errorCount: number; warningCount: number; message?: string; focusKey?: string; focusCode?: string; focusSeverity?: 'error' | 'warning' }
export interface WorkflowVersion { version: number; status: string; graph_hash: string; created_by: string; created_at: string; published_at: string | null; graph?: WorkflowGraph }
export interface Workflow { id: string; assistant_id: string; name: string; description: string; scope: 'private' | 'team'; owner_id: string | null; revision: number; draft_version: number; published_version: number | null; created_at: string; updated_at: string; status: 'draft' | 'published'; versions: WorkflowVersion[]; synthetic_only: boolean; execution_modes?: string[]; model_execution_enabled?: boolean }
export interface WorkflowTemplate { template_id: string; name: string; description: string; synthetic: true; graph: WorkflowGraph }
export interface WorkflowApproval { id: string; workflow_run_id: string; workflow_id: string; node_id: string; status: 'pending' | 'deciding' | 'approved' | 'rejected'; decision: 'approve' | 'reject' | null; comment: string; created_at: string; decided_at: string | null; decided_by?: string | null }
export interface WorkflowRunSummary { id: string; workflow_id: string; version: number; status: string; synthetic: boolean; execution_mode?: 'synthetic' | 'model' | 'tool'; trace_count: number; approval_node_id?: string | null; error?: string | null; created_at: string; completed_at: string | null }
export interface WorkflowRun { id: string; workflow_id: string; conversation_id?: string | null; version: number; graph_hash: string; permission_digest: string; permission_snapshot?: Record<string, unknown>; status: string; synthetic: boolean; execution_mode?: 'synthetic' | 'model' | 'tool'; approval?: WorkflowApproval | null; approvals?: WorkflowApproval[]; output: Record<string, unknown>; error?: string | null; tool_evidence_records?: Array<{ node_id: string; connection_id: string; tool_name: string; evidence: Array<Record<string, unknown>>; digest: string; created_at: string }>; trace: Array<{ sequence: number; node_id: string; node_type: WorkflowNodeType; status: string; external_call: boolean; approval_required: boolean; branch_skipped?: boolean; condition_result?: boolean; selected_targets?: string[]; model_id?: string; model_label?: string; reasoning_level?: string; role?: string; model_attempts?: number; model_retries?: number; model_retry_trace?: Array<{ attempt: number; status: string; error_code?: string; duration_ms?: number; retry_backoff_ms?: number }>; duration_ms?: number; input_bindings?: Array<{ key: string; source: string }>; structured_output?: boolean; structured_fields?: string[]; tool_call?: boolean; tool_name?: string; connection_id?: string; evidence_count?: number; output_preview?: string; error_code?: string }>; created_at: string; completed_at: string | null }
export interface Conversation { id: string; assistant_id: string; title: string; archived: boolean; active_run_id: string | null; created_at: string; updated_at: string; model_id: string; model_label: string; reasoning_level: string; runtime_type?: 'claude' | 'harness'; auto_model_routing?: boolean; has_native_session?: boolean; category?: string; tags?: string[] }
export interface Message { id: string; conversation_id: string; role: 'user' | 'assistant'; text: string; file_ids: string[]; run_id: string | null; created_at: string; citations?: EvidenceSource[]; skills?: Array<{id:string;name:string;revision:number}> }
export interface Skill extends ScopedResource { id: string; name: string; description: string; instructions: string; enabled: boolean; revision: number; required_tools: string[]; resources?: Record<string,string> }
export type PluginPermission = 'workspace.files.read' | 'workspace.files.write' | 'knowledge.read' | 'web.read' | 'mcp.connection.read' | 'artifact.write' | 'memory.read';
export type PluginStatus = 'pending' | 'approved' | 'revoked';
export interface PluginManifest { id:string; name:string; version:string; source_url:string; license:string; package_sha256:string; declared_permissions:PluginPermission[]; status:PluginStatus; review_note:string; revision:number; created_at:string; updated_at:string; approved_at:string|null; revoked_at:string|null }
export interface PluginEvent { id:number; action:'registered'|'approved'|'revoked'; actor_id:string; details:Record<string,unknown>; created_at:string }
export interface KnowledgeBase extends ScopedResource { id:string; name:string; description:string; enabled:boolean; default_for_new_assistants:boolean; document_count:number; index_status?:string }
export interface KnowledgeImageRegion { id:string; document_id:string; version:number; row:number; column:number; rows:number; columns:number; x:number; y:number; width:number; height:number; thumbnail_width:number; thumbnail_height:number; thumbnail_url:string; visual_index_status:string; entity_index_status:string; evidence_boundary?:string }
export interface KnowledgeImageAsset { width:number; height:number; bytes:number; thumbnail_width:number; thumbnail_height:number; thumbnail_url:string; region_count?:number; regions_url?:string; grid?:{rows:number;columns:number;max_regions:number}; regions?:KnowledgeImageRegion[]; metadata:Record<string,unknown>; ocr_status:string; visual_index_status:string; evidence_boundary?:string }
export interface KnowledgePagePreview { page:number; width:number; height:number; thumbnail_url:string }
export interface KnowledgeDocument { id:string; kb_id:string; name:string; mime_type:string; version:number; sha256:string; status:string; error?:string|null; page_count:number; chunk_count:number; embedding_model?:string; created_at:string; image?:KnowledgeImageAsset; page_previews?:KnowledgePagePreview[] }
export type KnowledgeRetrievalMode = 'hybrid' | 'bm25' | 'vector' | 'hipporag';
export interface KnowledgeRetrievalTrace {
  algorithm?: string;
  source_count?: number;
  unique_text_count?: number;
  returned_count?: number;
  graph_seed_count?: number;
  damping?: number;
  max_hops?: number;
  seed_nodes?: string[];
  seed_count?: number;
  node_count?: number;
  edge_count?: number;
  visited_node_count?: number;
  score_count?: number;
  fallback?: string | null;
  seed_entities?: Array<{ id?: string; label?: string; kind?: string; score?: number }>;
  expanded_entities?: Array<{ id?: string; label?: string; kind?: string; score?: number }>;
  paths?: Array<{ source?: string; target?: string; relation?: string; score?: number }>;
  iterations?: number;
  restart_probability?: number;
  truncated?: boolean;
}
export interface KnowledgeSeedEvidence {
  document_id?: string;
  version?: number;
  page?: number | null;
  chunk_id?: string;
  title?: string;
  label?: string;
  excerpt?: string;
  score?: number;
  content_url?: string;
}
export interface KnowledgeRetrieval { mode:KnowledgeRetrievalMode; available_modes?:KnowledgeRetrievalMode[]; lexical:string; vector_model:string; semantic:boolean; execution:string; visual_embedding?:Record<string,unknown>; algorithm?:string; backend?:string; fallback?:string; graph_trace?:KnowledgeRetrievalTrace; seed_evidence?:KnowledgeSeedEvidence[] }
export interface KnowledgeSearchResponse { results:KnowledgeHit[]; retrieval:KnowledgeRetrieval; context:{text:string;characters:number;truncated:boolean} }
export interface KnowledgeMediaEvidence { kind:'image'|'page_preview'; document_id:string; version:number; sha256:string; mime_type?:string; width:number|null; height:number|null; page?:number|null; thumbnail_url:string|null; regions?:KnowledgeImageRegion[]; ocr_status?:string; visual_index_status:string; evidence_boundary:string }
export interface KnowledgeHit { document_id:string; name?:string; title?:string; text?:string; excerpt?:string; page?:number; section?:string; version?:number; score?:number; rank?:number; lexical_score?:number; vector_score?:number; graph_score?:number; graph_trace?:KnowledgeRetrievalTrace; retrieval_algorithm?:string; evidence_type?:string; retrieval_mode?:KnowledgeRetrievalMode; url?:string; original_url?:string; source_url?:string; locator?:Record<string,unknown>; content_url?:string; media?:KnowledgeMediaEvidence }
export interface KnowledgeGraph { versioned:boolean; source:string; query:string; quality?:{status:string;quality_scope?:string;reviewed_entity_count?:number;reviewed_edge_count?:number;source_resolution_rate?:number;unresolved_entity_count?:number;candidate_noise_rate?:number|null;core_entity_recall?:number|null;acceptance_targets?:{candidate_noise_rate:number;core_entity_recall:number};note?:string;source_documents?:string[]}|null; nodes:Array<{id:string;label:string;kind:string;confidence?:number;evidence_boundary?:string;source:{document_id:string;version:number;page:number|null;paragraph:number|null;start:number|null;end:number|null;region_id?:string|null}}> ; edges:Array<{id:string;source:string;target:string;relation:string;confidence?:number;evidence_boundary?:string;provenance:{document_id:string;version:number;page:number|null;paragraph:number|null;start:number|null;end:number|null;region_id?:string|null}}>; multimodal?:{images:Array<{document_id:string;version:number;name:string;width:number;height:number;thumbnail_width:number;thumbnail_height:number;ocr_status:string;visual_index_status:string;region_count?:number;regions_url?:string;regions?:KnowledgeImageRegion[]}>;visual_embedding:string;entity_extraction:string;region_segmentation?:string;evidence_boundary:string} }
export interface EvidenceSource { id:string; number?:number; kind:'data'|'knowledge'|'web'; title:string; locator:Record<string,unknown>; excerpt:string; created_at:string; original_url?:string; document_id?:string; document_version?:number; content_url?:string }
export interface RunTimings { runtime_ready_ms?:number; first_server_progress_ms?:number; first_tool_attempt_ms?:number; first_tool_success_ms?:number; first_model_event_ms?:number; first_text_ms?:number; total_ms?:number }
export interface StoredFile { id: string; name: string; mime_type: string; size: number; created_at: string }
export interface Approval { id: string; run_id: string; tool_name: string; arguments: Record<string, unknown>; status: string; created_at: string; expires_at: string; risk_level?: 'low' | 'medium' | 'high'; approval_policy?: string; impact?: string; argument_digest?: string; decided_at?: string | null; decided_by?: string | null; decision_source?: string | null; strategy?: 'user' | 'model' | 'dual'; approval_mode?: 'single' | 'dual'; required_approvals?: number; approver_ids?: string[]; approval_policy_id?: string | null; approval_policy_revision?: number | null; approval_decision_reason?: string | null }
export interface ModelApprovalPolicy { assistant_id: string; enabled: boolean; reviewer_mode: 'synthetic'; model_id: string; allowed_tools: string[]; allowed_data_scopes: string[]; max_amount: number; allowed_operations: string[]; revision: number; updated_at?: string | null; updated_by?: string | null }
export interface HighRiskApprovalPolicy { assistant_id: string; enabled: boolean; policy_id: string; allowed_tools: string[]; admin_allowlist: string[]; required_approvals: 2; revision: number; updated_at?: string | null; updated_by?: string | null }
export type RunStatus = 'queued' | 'running' | 'awaiting_approval' | 'cancelling' | 'succeeded' | 'failed' | 'cancelled' | 'interrupted';
export interface NativeContext {
  source: 'native_runtime' | 'harness_token_meter'; status: 'available' | 'partial' | 'unavailable' | 'not_measured';
  total_tokens: number | null; max_tokens: number | null; percentage: number | null;
  auto_compact_enabled: boolean | null; auto_compact_threshold: number | null;
  model: string | null; measured_at: string | null; cached: boolean; error: string | null;
  precision?: 'native_reported' | 'estimated' | 'unknown';
  baseline_kind?: 'usage' | 'estimated' | 'none' | null;
  observed_prompt_tokens?: number | null; observed_at?: string | null;
  stale?: boolean; stale_reason?: 'route_changed' | 'request_unobserved' | 'snapshot_expired' | null;
  projection_seq?: number | null; manual_compact_available?: boolean;
  breakdown?: { system_tokens: number | null; tools_tokens: number | null; message_tokens: number | null } | null;
  last_compaction?: { run_id: string; status: CompactionStatus; trigger: string | null; pre_tokens: number | null; created_at: string; error?: string | null; followup_status?: string | null } | null;
}
export type CompactionStatus = 'started' | 'completed' | 'not_needed' | 'failed' | 'cancelled' | 'interrupted' | 'unknown';
export interface CompactionInfo { status: CompactionStatus; trigger: string | null; pre_tokens: number | null; before: NativeContext | null; after: NativeContext | null; error?: string | null; followup_status?: string | null }
export interface Run { id: string; conversation_id: string; status: RunStatus; execution_kind?: 'product_metadata' | 'model'; identity_source?: 'run_selection' | null; model_call_count?: number | null; kind?: 'chat' | 'compact'; compaction?: CompactionInfo | null; assistant_message_id: string | null; error: string | null; usage: Record<string, unknown> | null; approvals: Approval[]; created_at: string; completed_at: string | null; timings?:RunTimings }
export interface UsageSummary { total_runs: number; successful_runs: number; input_tokens: number; output_tokens: number; estimated_cost_usd: number | string | null; currency: string; users: Array<Record<string, unknown>> }
export interface StreamEvent { id: number; run_id: string; type: string; created_at: string; data: Record<string, unknown> }
export const isActive = (status: RunStatus) => ['queued', 'running', 'awaiting_approval', 'cancelling'].includes(status);
export const statusLabels: Record<RunStatus, string> = { queued: '等待执行', running: '正在执行', awaiting_approval: '等待你的确认', cancelling: '正在停止', succeeded: '已完成', failed: '执行失败', cancelled: '已停止', interrupted: '执行已中断' };
