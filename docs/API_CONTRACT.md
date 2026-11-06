# Client/server contract — 0.4

Status: implemented interface baseline, 2026-11-05; deployment acceptance is recorded separately in DELIVERY_VERIFICATION.md. API prefix `/api/v1`. Chinese product default brand: `知行工作台` (configurable). Client is a React/TypeScript Web application. Server is FastAPI, Python >=3.10, one process, SQLite persistence. One organization per deployment; admin/member roles; ownership checked on every resource. Inference uses official Python Agent SDK with configurable provider, currently using the authorized Tencent TokenHub configuration. No production mock-model fallback.

## Common behavior

- JSON errors: `{ "error": { "code": "CODE", "message": "Safe human message" } }`.
- Auth: HttpOnly session cookie; same-origin deployment or exact configured origins. Client fetch uses credentials. Origin checks on unsafe requests. No default published admin password; bootstrap CLI creates one.
- IDs are opaque strings; datetimes ISO UTC. List endpoints return arrays (not envelopes). DELETE returns 204 or `{ok:true}`. Backend OpenAPI documents exact schema.
- Members own their conversations/files/runs and may configure private assistants, connections, knowledge bases and Skills. Administrators manage users and team resources; admin role does not reveal another member's private content or credentials. Each scoped resource exposes `owner_id`, `scope:private|team` and authoritative `can_edit`.
- Product configuration never returns model-provider keys or MCP secrets. `credential` is write-only, omission retains the previous value, `clear_credential:true` removes it.

## Models

`User`: id, username, display_name, role (`admin|member`), enabled.

`Connection`: id, name, transport (`http`), url, auth_type (`none|bearer`), credential_present, enabled, status (`untested|connected|needs_auth|failed`), tools (array `{name,description,inputSchema}`), last_error (nullable string), last_tested_at (nullable), revision, source_metadata (bounded logical provenance description map), owner_id, scope, can_edit. Only show genuinely implemented authentication options. Personal OAuth is not yet a confirmed customer requirement.

`Assistant`: id, name, description, instructions, enabled, access `{all_members:boolean,user_ids:string[]}`, connections `[{connection_id,allowed_tools:string[],auto_approved_tools:string[]}]`, revision, knowledge_base_ids, skill_ids, web_search_enabled, default_model_id, default_reasoning_level, owner_id, scope, can_edit.

`Conversation`: id, assistant_id, title, archived, active_run_id (nullable), created_at, updated_at, model_id, model_label, reasoning_level, has_native_session (boolean), category (string), tags (string array). The selection fields are public names, never upstream deployment identifiers; the native-session boolean exposes readiness without exposing its internal ID.

`Message`: id, conversation_id, role (`user|assistant`), text, file_ids:string[], run_id (nullable), created_at, skills (`[{id,name,revision}]` snapshots on user messages), citations (minimal registered source summaries on assistant messages).

`Run`: id, conversation_id, status (`queued|running|awaiting_approval|cancelling|succeeded|failed|cancelled|interrupted`), assistant_message_id (nullable), error (nullable safe string), usage (nullable object), approvals (pending approval objects), created_at, completed_at, model (public label), model_id, reasoning_level, provider (public service label), timings (observed stage milliseconds). Historic runs without a selection snapshot use `历史模型`, `legacy-model`, and a null reasoning_level rather than guessing a route.

`File`: id, name, mime_type, size, created_at. Download URL is `/api/v1/files/{id}/content`.

`Approval`: id, run_id, tool_name, arguments (JSON object), status, created_at, expires_at. Decision approves immutable server-side args only.

`UsageSummary`: total_runs, successful_runs, input_tokens, output_tokens, estimated_cost_usd (nullable), currency (`USD`), users (array of user aggregates, admin only). When provider accounting isn't reliable, mark cost unknown rather than zero or applying Claude pricing to DeepSeek.

## Routes

| Method | Path | Request/response |
|---|---|---|
| GET | /config | `{brand_name,upload_max_bytes,auth_types:["none","bearer"],model_label,default_model_id,default_reasoning_level}` |
| GET | /models | Authenticated `ModelOption[]`; public model labels, availability and supported reasoning levels |
| GET | /health/live, /health/ready | health JSON |
| POST | /auth/login | `{username,password}` → User plus session cookie |
| GET | /auth/me | User |
| POST | /auth/logout | clear cookie |
| GET/POST | /users | User[] / `{username,password,display_name,role}` → User |
| PATCH | /users/{id} | `{display_name?,role?,enabled?,password?}` → User |
| GET/POST | /connections | Connection[] / connection fields plus credential → Connection |
| GET/PATCH/DELETE | /connections/{id} | Connection / update / soft delete |
| POST | /connections/{id}/test | Connection including tools/status/error |
| GET/POST | /assistants | Assistant[] / assistant fields → Assistant |
| GET/PATCH/DELETE | /assistants/{id} | Assistant / update / soft delete |
| GET/POST | /conversations | Conversation[] / `{assistant_id,title?,model_id?,reasoning_level?,category?,tags?}` → Conversation |
| GET/PATCH | /conversations/{id} | Conversation / `{title?,archived?,model_id?,reasoning_level?,category?,tags?}` |
| GET | /conversations/{id}/messages | Message[] |
| POST | /conversations/{id}/messages | `{client_request_id,text,file_ids:[],skill_ids:[]}` → 202 `{message,run_id}` |
| GET | /runs/{id} | Run |
| GET | /runs/{id}/trace | Owner-authorized sanitized JSON: run metadata/timing/usage, linked messages and persisted product events |
| GET | /runs/{id}/events?after=0 | replayable SSE; Last-Event-ID supported |
| POST | /runs/{id}/cancel | Run or `{ok:true}` |
| POST | /approvals/{id}/decision | `{decision:"allow"|"deny"}` → approval status |
| POST | /files | multipart field `file` → File |
| GET | /files/{id}, /files/{id}/content | metadata / authorized bytes |
| GET | /usage | UsageSummary |

## Model selection and private routing

`GET /models` returns an array of `{id:string,label:string,available:boolean,unavailable_reason?:string,reasoning_levels:[{id:string,label:string}],default_reasoning_level:string,requires_new_conversation_on_switch:boolean}`. The current public catalog contains `deepseek-v4.1-flash`, `glm-5.3` / `GLM-5.3`, `dano-v2.1` / `Dano-v2.1`, `hy4` / `混元 4`, and `kimi-k3.1-pro` / `Kimi 3.1 PRO`. The last two are present in the catalog but remain unavailable until an administrator supplies a valid enabled upstream route. Dano-v2.1 is Better AI's industrial assistant product. There is always at least one reasoning option. Models with no selectable effort expose `[{id:"default",label:"默认"}]`, mapped internally to an omitted SDK effort option.

Creating a conversation without an explicit selection uses the assistant defaults first, then the deployment defaults. The conversation remains bound to this assistant; its assistant_id is not patchable. A model change without `reasoning_level` uses that model's default. A change of only `reasoning_level` retains the current model. Dano's levels are `light`, `high`, `max`, privately mapped to upstream `low`, `high`, `max`. An unknown ID returns `422 UNKNOWN_MODEL`; a known but disabled/unconfigured deployment returns `503 MODEL_UNAVAILABLE`; an unsupported level returns `422 UNSUPPORTED_REASONING_LEVEL`. Failed changes do not alter the saved selection.

GLM-5.3 currently sets `requires_new_conversation_on_switch:true`: use it in a new conversation. If a conversation has a native SDK session and a PATCH changes the public model ID, a protected old or new model returns `409 MODEL_NEW_CONVERSATION_REQUIRED` with `请新建对话使用此模型；原对话和记忆保留`. This rule is bidirectional, including historical public aliases whose private upstream route matches a protected catalog entry. It does not erase context, silently fork, or change the original model. An empty conversation without a native session may select GLM normally; subsequent turns within its GLM session remain available. Same-model reasoning changes and Dano/DeepSeek switching remain available. The client uses `has_native_session` and this public capability to explain the limitation before submitting a forbidden switch. This boundary follows observed repeated GLM continuation timeouts on cross-model history, while a fresh GLM conversation succeeded; it is not a general claim that the provider can never support such history.

Selection is persisted per conversation. Changing model or reasoning while a run/approval is active returns `409 CONVERSATION_BUSY`; while native context measurement is active it returns `409 CONTEXT_BUSY`. A successful switch clears the old context statistics and changes the runtime fingerprint. The next operation rebuilds the native SDK client with the existing session ID; message history and the conversation memory scope remain available. Each prepared run freezes its selection privately, so its trace retains the original public model after later switches. The selection also pins a SHA-256 digest of the effective provider environment (global settings plus per-model overrides and selected model aliases); credentials are never copied into database snapshots. Endpoint/key/configuration changes require explicit reselection and cannot silently retarget a prepared run. Runtime initialization uses only the validated in-memory environment, never a second dynamic configuration-file read.

`AGENT_MODEL_CATALOG` points to a private, server-only JSON document; `AGENT_DEFAULT_MODEL_ID` optionally overrides its default. The schema is demonstrated in `server/model-catalog.example.json`. Each model entry contains public `id`/`label`, private `upstream_model`, `enabled`, a `reasoning_levels` object mapping public levels to SDK effort strings or null, and `default_reasoning_level`. The optional boolean `requires_new_conversation_on_switch` defaults to false and is true for GLM in the template. Optional per-model `provider_env` overrides may contain only `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY`, and `ANTHROPIC_AUTH_TOKEN`. Keep catalog files with credentials owner-readable only; no catalog internals are sent to the client. Catalog changes take effect on service restart.

Historical conversations migrate from their last recorded upstream model and the prior deployment's configured effort. Keep the pre-upgrade `AGENT_MODEL`/`AGENT_EFFORT` during migration. When this exact pair does not match a new public option, the conversation retains its previous private route and displays `历史模型`. A historical route that no longer matches the retained deployment configuration is blocked with `409 MODEL_SELECTION_REQUIRED` until the owner selects a model. Likewise, changing a catalog mapping does not silently reroute existing conversations: explicit reselection is required. A deployment with no catalog retains single-model compatibility under public `legacy-model` / `默认模型`. JSON selections created before provider digests existed are upgraded using the current provider configuration; no SQL column change is needed. Old completed-run metadata is not rewritten. Historical credential identity was never recorded and cannot be retroactively verified; provider-digest guarantees begin at the upgrade. In-flight tasks at startup remain interrupted and are never replayed.

Public run metadata, context snapshots and structured context events use public labels, including sanitized historical compaction records. Private execution metadata and raw SDK records stay server-side. This is a metadata boundary and is not a promise that every possible model-generated text can be made secret by a prompt.

## Stream protocol

Each persisted event: `{id:number,run_id,type,created_at,data}`. SSE `id` is the per-run number, `event` is the type, `data` is the JSON event object. Client can use EventSource with explicit event listeners; stream reconnect preserves last event ID. The server persists before emission; disconnect does not cancel execution. Terminal runs return after replay, and clients close upon run.completed.

- `run.started`: `{}`
- `message.delta`: `{message_id,text}`; no duplicate complete-message append.
- `message.updated`: `{message_id,text,citations}` replaces draft text after canonical source finalization; do not append it to deltas.
- `run.stage`: `{stage}` where stages include initializing, runtime_ready, model_started, answering and tool.
- `timing.updated`: observed `runtime_ready_ms`, `first_model_event_ms`, `first_text_ms`, and later `total_ms`. Missing values remain absent.
- `tool.started`: `{tool_call_id,tool_name,arguments}`; only safe/redacted args.
- `tool.completed`: `{tool_call_id,tool_name,success,summary?}`.
- `approval.required`: Approval.
- `approval.resolved`: `{approval_id,decision}`.
- `artifact.created`: File.
- `usage.updated`: usage object.
- `run.completed`: `{status,message_id,error?,usage?,timings?}`.

On terminal event, refetch canonical messages/conversation/run. Active run is returned when reopening a conversation. Same client_request_id returns original message/run, concurrent different request returns 409 CONVERSATION_BUSY. Startup marks in-flight runs interrupted and approvals invalid; never replay external writes automatically.

## MCP and execution constraints

- Remote Streamable HTTP MCP supports none/bearer authentication; the deployment administrator maintains allowed destination hosts. Members manage their private connections and administrators manage team connections. A separate fixture MCP service is included for deterministic acceptance.
- Test connects and discovers tools from the execution environment without executing business mutations.
- Configure connections/assistants in the client; backend injects allowed config/credentials into per-conversation SDK sessions. Cwd/settings/transcripts isolated, no global user hooks/settings loading. Import only explicit provider environment keys from an authorized server-side config file.
- No arbitrary user-supplied stdio commands or Bash/Agent tool access. Public web retrieval is a guarded product capability enabled per assistant, rather than unrestricted native WebFetch/WebSearch access. File operations remain within the current conversation workspace or the current conversation's native memory directory; other users, assistants and hidden SDK configuration are denied independently of approval.
- Runtime evaluates current user/assistant/connection/tool grants on every tool call, not only when creating the session. Hook failures deny execution. `allowed_tools` auto-approval is distinct from authorization.
- Session idle reap, interrupt/drain, explicit SDK session ID resume and persisted workspace are required. SDK result estimates must not be assumed to be actual DeepSeek billing.

## Delivery and acceptance

Separate `client/` and `server/`, their own build/dependency files and Dockerfiles; top-level compose/reference deployment and docs. The remote server has no Docker installed, so also support Python virtualenv plus static client server without modifying unrelated services. Root coordinates integration and real remote acceptance.

## Native memory and context additions

These remain cookie-authenticated product APIs under `/api/v1`. There is no OpenAI compatibility API or API-key layer. The client and server own the complete agent execution, context and native memory workflow.

Normal chat sends composed user text and any explicitly selected Skill instructions through the pinned SDK's per-turn `client_composed:true` flag. Native slash-command dispatch and `@path`/MCP mention expansion are disabled for those turns, so text cannot read an arbitrary local file before the tool guard. Uploaded files remain available through authorized workspace Read calls. Only the server-constructed manual-compaction operation uses command dispatch. This SDK mode skips turn-start attachment expansion; native session initialization and later tool-call context behavior remain managed by the runtime, and are included in the real-memory acceptance boundary.

### Independent conversation memory (0.4.2)

The scope is one conversation owned by the signed-in user. Administrative role does not reveal another user's memory. Native `autoMemoryDirectory` points to `AGENT_DATA_DIR/conversation-memory/<user_id>/<conversation_id>/`. SDK configuration, workspaces, context, approvals and cancellation remain conversation-scoped. Two sessions of the same assistant can run concurrently within the global active-session capacity.

| Method | Path | Input / result |
|---|---|---|
| GET | /conversations/{conversation_id}/memory | Memory listing below |
| GET | /conversations/{conversation_id}/memory/file?path=MEMORY.md | `{path,content,revision,updated_at}` |
| PUT | /conversations/{conversation_id}/memory/file | `{path,content,revision}` → same file object |
| DELETE | /conversations/{conversation_id}/memory/file?path=...&revision=N | Updated memory listing |
| DELETE | /conversations/{conversation_id}/memory?revision=N | Updated empty memory listing |

Memory listing: `{conversation_id,assistant_id,scope:"conversation",origin:"empty"|"legacy_snapshot",read_only:false,revision:number,busy:boolean,files:[{path,size,updated_at}],max_file_bytes:131072,max_total_bytes:1048576}`. Paths are relative Markdown filenames; traversal, symlinks, hidden paths and non-Markdown writes are rejected. Maximum100managed files. Quotas count UTF-8 bytes, including native Write/Edit operations.

Writes require the observed revision. `409 MEMORY_REVISION_CONFLICT` preserves the draft pending reload. `409 MEMORY_BUSY` applies only while this conversation runs or measures context; `409 CONVERSATION_BUSY` prevents a second active task in the same conversation. Another session of the same assistant is no longer blocked by `MEMORY_SCOPE_BUSY`. Native file revisions affect only this conversation's runtime fingerprint/context cache.

Upgrade creates durable `conversation_memory_scopes` rows: pre-existing conversations are marked `legacy_pending`, and their first use atomically publishes independent snapshots of the old user/assistant directory and records `legacy_snapshot`. Originals remain. New conversations atomically start `empty`; clearing or restarting does not re-import old files. Legacy `/assistants/{assistant_id}/memory` GET/list and GET/file remain read-only exports; PUT/DELETE return `403 MEMORY_LEGACY_READ_ONLY`. These files are never a live shared runtime directory.

Clearing memory does not erase the resumed conversation's history or already loaded context. New sessions start with independent empty memory; archiving a session retains its own memory. No additional summarizer is introduced.

### Authoritative model information (0.4.2)

A bounded allowlist of whole-message identity/model questions without attachments or selected Skills is answered from the run's frozen public selection. Dano answers Dano-v2.1; DeepSeek and GLM use their selected public labels. These are product metadata replies, not native model completions. No old message or native transcript is rewritten, no SDK session is reset, and no model request is made.

Run views expose `execution_kind:"product_metadata"|"model"`, `identity_source:"run_selection"|null`, and `model_call_count:0|null`. Metadata replies record zero Tokens and zero model cost with `usage.source:"product_metadata"`, `cost_is_estimate:false`. Ordinary model runs keep unknown provider metrics unknown. Metadata is present in the application history but not forged into SDK history. Quoted, negated, mixed business, attachment, Skill and non-allowlisted requests use the actual selected model and current system policy. This deterministic route is not a claim that every possible model-generated answer is infallible.

### Native context reporting

`GET /conversations/{id}/context` reads a persisted snapshot and makes no provider request. `POST /conversations/{id}/context/refresh` has no body and explicitly invokes the active actor's public SDK `get_context_usage()`; a snapshot less than 60 seconds old is reused. An active run or approval returns `409 CONTEXT_BUSY`. A conversation without a native session returns unavailable statistics without starting a model query.

Context object: `{source:"native_runtime",status:"available"|"unavailable"|"not_measured",total_tokens:number|null,max_tokens:number|null,percentage:number|null,auto_compact_enabled:boolean|null,auto_compact_threshold:number|null,model:string|null,measured_at:ISO|null,cached:boolean,error:string|null,last_compaction:LastCompaction|null}`. `available` requires native total, limit and percentage fields; absent values remain null, and the service never calculates them from accumulated usage. Detailed private memory/config paths are omitted. A new native query invalidates the old snapshot.

Context measurement can make additional provider token-counting requests; it is not claimed to be a local or free operation for a compatible third-party provider. The client should request it only on explicit refresh, not on each SSE event. Measurement errors produce a safe unavailable object and do not fail an otherwise successful chat or compaction.

### Native manual compaction

`POST /conversations/{id}/compact` accepts `{client_request_id:string,instructions?:string}` and returns `202 {message,run_id}`. It sends the native `/compact` command (optionally with user focus instructions) through the same session actor; there is no separate model call or application summary pipeline. No native session yields `409 CONTEXT_NOT_READY`; execution/approval conflicts use the existing busy errors. Repeating a request ID returns its original run.

Run objects add `kind:"chat"|"compact"` and nullable `compaction:{status:"started"|"completed"|"not_needed"|"failed"|"cancelled"|"interrupted",trigger:string|null,pre_tokens:number|null,before:Context|null,after:Context|null,boundary_observed?:boolean,error?:string|null,followup_status?:"failed"|"cancelled"|"interrupted"}`. `completed` requires an actual native `compact_boundary` event. A successful ResultMessage without that event is `not_needed` (for example, insufficient history), never a claimed compression. Failure, cancellation and restart persist their terminal state. If a boundary already occurred, compaction remains `completed` and `followup_status` records interruption of subsequent counting or completion steps; cancellation cannot undo compaction. Before/after snapshots may be unavailable.

New persisted SSE events:

- `context.compaction_started`: `{trigger:"manual"|"auto"|null}`.
- `context.compacted`: `{trigger:"manual"|"auto"|null,pre_tokens:number|null}`, emitted only for native `compact_boundary`, including native automatic compaction.
- `context.compaction_result`: the manual run's compaction object, including terminal failures.

The final event remains `run.completed`; refetch canonical run/messages as usual. `last_compaction` is reconstructed from persisted events as `{run_id,status:"completed"|"not_needed"|"failed"|"cancelled"|"interrupted",trigger,pre_tokens,created_at,error:string|null,followup_status:string|null}`, so subsequent normal chats do not hide the most recent result. This record reports an observed boundary or operation outcome, not a guarantee that original facts have been removed from session history.

SDK evidence: [native /compact command](https://code.claude.com/docs/en/agent-sdk/slash-commands#compact-history-with-compact), [Python context API](https://code.claude.com/docs/en/agent-sdk/python#contextusageresponse), [native automatic memory](https://code.claude.com/docs/en/memory#enable-or-disable-auto-memory). The pinned Python SDK supports PreCompact; boundary messages provide completion evidence without relying on an unsupported Python PostCompact hook.


## Configurable resources and sessions — 0.4

Connections, assistants, knowledge bases and Skills have `scope:"private"|"team"`, server-derived `owner_id`, and `can_edit`. Create defaults to private. Members may create/edit their own private resources; only administrators create/edit team resources. Private resources remain inaccessible to other users, including administrators. Clients cannot assign ownership. Legacy connections and assistants migrate to team scope with existing grants retained.

Assistant bindings must refer to resources usable by the current owner. Team assistants cannot bind private dependencies. An omitted `knowledge_base_ids` on new-assistant creation uses the enabled team collections marked `default_for_new_assistants`; an explicit empty list binds none. Editing defaults does not automatically rewrite existing assistants. `GET /assistants?manage=true` also includes disabled editable assistants; normal listing includes usable assistants. Resource changes and current permissions are checked by the server when preparing and executing work.

From client/server0.4.1, `PATCH /assistants/{id}` accepts optional `expected_revision` (integer >=1). The client sends the revision loaded with its draft. A stale revision, a concurrent save between validation and update, or a concurrent deletion returns `409 ASSISTANT_CONFLICT`; no draft fields are written. Reload explicitly after preserving or discarding the local draft. Existing clients may omit the field, but concurrent read/merge/write still uses the row's observed revision in an atomic conditional update. Omitted patch fields remain unchanged; explicit empty binding arrays remove those bindings, and null default-model/default-reasoning values clear those overrides. This adds no schema migration.

`GET /conversations` accepts optional `q` (max200), `assistant_id`, `archived` (boolean), and `category` (max100). Search covers the owner's title, message content, category and tags. Responses are bounded to1000 rows. Category is one string; tags are at most10 nonempty strings of at most40 characters. PATCH cannot change assistant_id. `archived:false` restores a conversation; archiving preserves messages and attachments.

### MCP import

`POST /connections/import/preview` and `POST /connections/import` accept `{config:object|array,scope?:"private"|"team"}`. Preview returns `{format,connections,warnings}` with masked credential presence. Commit creates all connections atomically and returns `{connections,warnings,binding_required:true}`. The import does not bind assistants or approve tools; imported tool/approval fields are suggestions. Limits:16 connections and256KiB UTF-8 input. Scope rules apply independently of import formatting.

Connection `source_metadata` is a map of bounded string descriptions, for example logical database/schema/tables/fields/timezone/purpose. It does not replace per-query result provenance and is not a SQL execution API. Current MCP protocols and customer credentials remain authoritative.

### Skills

| Method | Path | Input / result |
| --- | --- | --- |
| GET/POST | /skills | Skill[] / create Skill |
| GET/PATCH/DELETE | /skills/{id} | Read, create new revision, or soft delete |
| GET | /skills/{id}/versions | Authorized saved revision snapshots |
| POST | /skills/import | `{markdown,name?,scope?}` imports declarative SKILL.md |
| GET | /assistants/{id}/skills | Enabled Skills usable by this assistant and user |

Skill fields: `id,name,description,instructions,required_tools:string[],resources:Record<string,string>,enabled,revision,owner_id,scope,can_edit,created_at,updated_at`. Import accepts simple display metadata and instructions; it never executes installation scripts. Declared tools do not enlarge the assistant allowlist.

Messages accept up to5 unique `skill_ids`. The server validates assistant binding, availability and authorization, snapshots the selected revisions for that run, and records `{id,name,revision}` on the user message. The client sends stable IDs from its authorized picker; typed `@path` does not load a server file. Skills apply to the submitted turn. A Skill-only message may omit text and files. A stopped/revoked Skill cannot execute from an old client selection.

### Knowledge collections, files and retrieval

| Method | Path | Input / result |
| --- | --- | --- |
| GET/POST | /knowledge-bases | KnowledgeBase[] / create collection |
| GET/PATCH/DELETE | /knowledge-bases/{id} | Authorized read, update or soft delete |
| GET/POST | /knowledge-bases/{id}/documents | Latest document versions / multipart `file` upload |
| POST | /knowledge-bases/{id}/documents/{document_id}/versions | Multipart new immutable version |
| GET | /knowledge-documents/{id}/versions | Authorized document version array |
| GET | /knowledge-documents/{id}/chunks?version=N | Authorized extracted chunks for the selected version |
| GET | /knowledge-documents/{id}/content?version=N | Authorized original bytes with correct MIME |
| POST | /knowledge-documents/{id}/reindex | Rebuild current version index; returns document status |
| DELETE | /knowledge-documents/{id} | Soft delete file and remove access from retrieval/source views |
| POST | /knowledge-bases/{id}/search | `{query,limit?}` → `{results,retrieval}` |

KnowledgeBase fields include `id,name,description,scope,owner_id,can_edit,enabled,revision,default_for_new_assistants,document_count,index_status,retrieval`. Only administrators may mark an enabled team knowledge base as a default. `index_status` is a count map by status, not a fabricated completion percentage.

Document metadata includes `id,kb_id,name,mime_type,version,sha256,size,status,error,page_count,chunk_count,embedding_model,extraction,created_at,content_url`. Status is `processing`, `ready`, `needs_ocr`, or `failed`. A failed or empty extraction is not ready. Originals and prior versions are retained separately; uploads keep the document format. File sizes follow deployment upload limits; the parser also bounds PDF pages and extracted text. Physical page numbers apply only to PDFs. Text/DOCX content uses sections, paragraph indices and extraction offsets.

Search result fields include `document_id,version,name,title,text,excerpt,page,section,paragraph,start,end,chunk_id,score` and knowledge-base metadata. Retrieval reports `mode`, `lexical`, `vector_model`, `semantic` and `execution`. BM25 combines with the configured local vector method; `local-lexical-v1` is explicitly nonsemantic. Run admission pins visible document versions; retrieval and source reopening also recheck current authorization. The application supplies native targeted-read and retrieval tools through the same guarded runtime. See [knowledge implementation and deployment](KNOWLEDGE_AND_SOURCES.md).

### Registered evidence and references

`GET /runs/{id}/sources` returns an array of authorized source details; `GET /sources/{id}` returns one authorized source. Source kinds are `data`, `knowledge`, and `web`. Detailed fields include `id,kind,title,locator,excerpt,created_at,url,original_url?,document_id?,document_version?,content_url?`. The locator is structured actual provenance, with public logical names instead of server filesystem paths or credentials.

Data locators include available database/schema/table/fields/records/filters/time range/timezone/query time/aggregation; missing fields are marked missing. Knowledge locators identify original file/hash/version and a real PDF page or extracted paragraph. Web locators identify original URL, fetch time, excerpt/content type, and publication time only when observed. Chat attachments register their own file content URL and ownership, rather than pretending to be a knowledge-base document.

Only actual tool results register opaque source IDs. The model references `[[source:ID]]`; finalization validates it and creates numbered Markdown links plus a canonical footer. Unrelated conversation IDs and model-authored raw internal source links cannot create references. Historical sources from the same conversation are reauthorized before reuse. Persisted message citations keep minimal `id,number,kind,title,url,document_id?,document_version?`; sensitive locator/excerpt details are fetched from the source endpoint on demand.

The client makes internal source controls clickable only when their canonical ID is registered in the current message citations. Unverified or encoded internal source links remain inert. Source access checks the run owner and current resource/assistant/tool permission. Original answer text remains historical conversation content after revocation, while source details and original bytes are denied. PDF original links include the saved version and optional `#page=N`. Attachment links are restricted to `/api/v1/files/{id}/content`.

### Public web capability and timing

`Assistant.web_search_enabled` controls guarded search and page reading. The current search provider is Bing RSS; page fetches validate public DNS and redirects, bound content/time, reject private/local addresses, and accept text content only. The runtime registers search snippets and fetched page text as distinct evidence. Search-provider availability is separate from model availability. No API key for web search is exposed to the client.

`Run.timings` and `timing.updated` use elapsed milliseconds from run admission for runtime readiness, first model event, first visible text and total completion. They measure observable client/server stages; they are not upstream queue-duration measurements. Tool event timing is retained with the actual operation. Missing historical timings remain unknown. Canonical `message.updated` must replace streamed draft text, and terminal clients refetch messages/run as usual.

### Advisory model fitness recommendation

`POST /api/v1/models/recommend` accepts `{ "task_summary": string, "candidate_model_ids"?: string[] }`. The summary is capped at 2,000 characters and candidates at 20 public catalog IDs. The response includes `{model_id,model_label,reasoning_level,reason,applied:false,router}`. The operation is advisory metadata and does not itself patch a conversation, replace an active run, create a native session, or add a run/usage row. The client may explicitly apply the returned selection only while the conversation is idle; ordinary model-switch guards remain authoritative. Unavailable or unknown candidate IDs are filtered; if none remain the server returns `NO_AVAILABLE_MODELS`.

The router identity is the public `deepseek-v4.1-flash` ID. The default bounded local scorer makes no provider call. With explicit administrator configuration, one provider call receives only the sanitized task summary and public catalog capabilities, with bounded timeout/output/budget and strict response validation; failures fall back to the local scorer. Provider credentials, private upstream model names, conversation history, attachments, assistant instructions and MCP data are never sent to the router. Applying a suggestion still uses normal conversation model-switch and new-conversation guards.
