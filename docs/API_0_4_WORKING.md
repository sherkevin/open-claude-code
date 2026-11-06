# 0.4 working integration contract

Implementation coordination; not a release acceptance record.

## Resource scope

Connections, assistants, skills, knowledge bases expose owner_id, scope (`private`/`team`), can_edit. Members create private resources; only admins create/edit team resources. Admin access to team resources does not implicitly reveal members' private credentials/content. Team resources are usable by authenticated members according to assistant access rules; only authorized resource owners/admins may edit them. Existing legacy connections/assistants migrate to team scope; keep access rules. Do not accept owner_id from client input.

## Core API additions (core-server owner)

- Assistant fields: knowledge_base_ids: string[], skill_ids: string[], web_search_enabled: boolean, default_model_id?:string, default_reasoning_level?:string, owner_id,scope,can_edit. Existing connections bindings retained. Only editable/usable authorized resources can be bound; team assistants cannot depend on someone else's private resource. Default company KB is injected for new assistants only after it exists and is explicitly marked default_for_new_assistants.
- Conversation fields: category:string (max100, default empty), tags:string[] (max10, item max40). GET /conversations supports q,assistant_id,archived,category; search title/message text for owned conversations, bounded response. Existing title/archived/model PATCH retained; assistant binding immutable. Session UI chooses assistant and lists its sessions.
- GET/POST /skills; GET/PATCH/DELETE /skills/{id}; fields id,name,description,instructions,scope,enabled,revision,owner_id,can_edit. Import SKILL.md via POST /skills/import with markdown,name?; no auto-execution of scripts from untrusted packages. Skills are versioned instructions with declared required_tools:string[] and optional resources text mapping (bounded); actual tool permissions remain assistant's existing allowlist. Structured message skill_ids:string[] (max5) and selected Skill revisions snapshotted for the run.
- GET /assistants/{id}/skills returns enabled skill metadata usable in composer. Existing /assistants list returns usable resources incl private owner resources; optional manage=true includes disabled editable rows.
- 0.4.1 assistant editing: `PATCH /assistants/{id}` accepts optional `expected_revision` (integer >=1) from detail GET. A stale revision or competing save/delete returns HTTP 409 `ASSISTANT_CONFLICT`; keep the draft and explicitly reload/review, never automatically overwrite with a newer revision. Omitted top-level fields are preserved; capability lists and `access`/connection bindings are full replacement values. Explicit null clears the two model default fields. Clients should send changed fields and the fetched revision. Server updates are also guarded against revision changes for legacy callers that omit the new field.
- Shared permission helpers on Service: resource_access(row,user), resource_editable(row,user), require_resource(table,id,user,edit=False). Core owns db.py/models.py/main.py/service.py/mcp_import.py and new resources.py/skills.py. Knowledge module uses these helpers.

## Knowledge and evidence API (knowledge owner)

- New module KnowledgeService(service), stored service.knowledge; includes its own idempotent table setup, no edits to core db.py. create_knowledge_router(svc,authenticated) imported/registered by root/core main.
- GET/POST /knowledge-bases; GET/PATCH/DELETE /knowledge-bases/{id}; name,description,scope,enabled,default_for_new_assistants(admin/team only),owner_id,can_edit,document_count,index status.
- GET /knowledge-bases/{id}/documents; multipart POST same path (file). Documents: id,kb_id,name,mime_type,version,sha256,status,error,page_count,chunk_count,created_at. Keep original versions immutable; POST /knowledge-bases/{id}/documents/{document_id}/versions optionally uploads replacement. Include searchable original text/PDF/DOCX/Markdown, OCR integration if available; parsing failure never a ready empty index. Owner upload and read ACL enforced.
- GET /knowledge-documents/{document_id}/content?version=... returns original with correct MIME, user authorization. GET /knowledge-documents/{document_id}/chunks exposes page/section/text for verification. POST /knowledge-bases/{id}/search {query,limit} as diagnostic/user search.
- EvidenceService(service), stored service.evidence; own tables. register(run_id,kind,title,locator,excerpt,resource...) returns source with stable opaque id. kind=`data`|`knowledge`|`web`. Source registration must tie run/conversation/user to authorized data/tool; arbitrary model text cannot create evidence.
- GET /runs/{id}/sources -> array of source summaries. GET /sources/{id} -> permission-checked details. Fields: id,kind,title,locator:object,excerpt,created_at,original_url?:string,document_id?:string. Public locator never contains credentials/internal service file paths. Source authorization checks run ownership AND source resource access, including after permissions change.
- Model receives markers [[source:ID]]. Server finalization maps only actually registered sources to canonical references (numbers) and Message.citations list. Footnotes for generated output must not trust arbitrary URLs/sourceIDs; strip or visibly flag invalid reference markers. Tool results are untrusted data, cannot change instructions.
- Runtime integration interface required: await knowledge.search_for_run(user,assistant,run_id,query,limit=6) -> small result snippets+source IDs; await knowledge.read_for_run(...,document_id,page?) -> targeted original text+source. Root wires through in-process SDK MCP tools.

## Runtime and client integration (root + client owner)

- Preserve native context, compaction and per-user/assistant memory. Root integrates knowledge/evidence/tools into runtime.py and concise identity. Search tool returns web evidence, optional assistant flag; HTTP fetch must validate public URLs/redirects and bounded responses.
- Timings expose runtime_ready_ms,first_model_event_ms,first_text_ms,total_ms plus per-tool timings when available; do not fabricate upstream-only timings.
- Client owns client/**. Use contract fields; capability pages for resources, assistant editor sections, session manager, @Skill chips, sources panel with type/locator/original and server authorization. Existing API errors/model boundaries preserved.
- Root owns integration docs, runtime/identity/search, example datasource provenance, OCR/company import, deployment/release. Core/knowledge owners notify contract changes before client implementation diverges.
