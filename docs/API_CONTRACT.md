# Client/server contract — 0.4

Status: implemented interface baseline, 2026-11-05; deployment acceptance is recorded separately in DELIVERY_VERIFICATION.md. API prefix `/api/v1`. Chinese product default brand: `知行工作台` (configurable). Client is a React/TypeScript Web application. Server is FastAPI, Python >=3.10, one process, SQLite persistence. One organization per deployment; admin/member roles; ownership checked on every resource. Inference uses official Python Agent SDK with configurable provider, currently using the authorized Tencent TokenHub configuration. No production mock-model fallback.

## Common behavior

- JSON errors: `{ "error": { "code": "CODE", "message": "Safe human message" } }`.
- Auth: HttpOnly session cookie; same-origin deployment or exact configured origins. Client fetch uses credentials. Origin checks on unsafe requests. No default published admin password; bootstrap CLI creates one.
- IDs are opaque strings; datetimes ISO UTC. List endpoints return arrays (not envelopes). DELETE returns 204 or `{ok:true}`. Backend OpenAPI documents exact schema.
- Members own their conversations/files/runs and may configure private assistants, connections, knowledge bases and Skills. Administrators manage users and team resources; admin role does not reveal another member's private content or credentials. Each scoped resource exposes `owner_id`, `scope:private|team` and authoritative `can_edit`.
- Product configuration never returns model-provider keys or MCP secrets. `credential` is write-only, omission retains the previous value, `clear_credential:true` removes it.

## OpenAI-compatible model adapter (local validation slice)

`server/app/openai_compat.py` defines the bounded internal adapter contract for
compatible model gateways. It accepts either a Chat Completions envelope
(`messages`) or a Responses envelope (`input`), normalizes both into a
`NormalizedRequest`, and renders protocol-specific non-streaming responses or
stream events. The injected transport receives only the normalized request;
the adapter has no HTTP client, does not read credentials, and cannot contact
an upstream model by itself. A configured model allowlist, message/tool/text
limits, supported multimodal text/image parts, function tool calls, and
provider-status mapping are enforced before and after transport execution.

The public error mapping is stable (`MODEL_AUTHORIZATION_FAILED`,
`MODEL_TIMEOUT`, `MODEL_RATE_LIMITED`, `MODEL_SERVICE_UNAVAILABLE`,
`MODEL_EXECUTION_FAILED`); upstream response bodies and diagnostics are never
returned. Usage is exposed only for non-negative integer fields actually
provided by the transport (`input_tokens`, `output_tokens`, `total_tokens`,
and optional cached-input tokens). This is a local synthetic compatibility
slice, not authorization for a real endpoint, API-key route, or production
deployment. `server/tests/test_openai_compat.py` covers both envelopes,
streaming/non-streaming, function calls, bounds, error mapping, and redaction.

## Per-conversation execution engine (Harness pilot)

Conversations expose `runtime_type` (`claude|harness`). It is fixed by the server when the conversation is created; create/patch requests cannot select an engine, launch path, or native session ID. Historical conversations remain Claude. `has_native_session` refers only to the selected engine. The send, event, approval, cancel, artifact, bounded-attachment and authorized business-MCP contracts are shared. Harness also exposes session-bound context statistics, controlled conversation memory and manual compaction; unsupported paths still fail explicitly rather than falling back to another engine. See [pilot boundaries](operations/HARNESS_PILOT.md).

## Models

`User`: id, username, display_name, role (`admin|member`), enabled.

`Connection`: id, name, transport (`http`), url, auth_type (`none|bearer`), credential_present, enabled, status (`untested|connected|needs_auth|failed`), tools (array `{name,description,inputSchema}`), last_error (nullable string), last_tested_at (nullable), revision, source_metadata (bounded logical provenance description map), owner_id, scope, can_edit. Only show genuinely implemented authentication options. Personal OAuth is not yet a confirmed customer requirement.

`Assistant`: id, name, description, instructions, enabled, access `{all_members:boolean,user_ids:string[]}`, connections `[{connection_id,allowed_tools:string[],auto_approved_tools:string[]}]`, revision, knowledge_base_ids, skill_ids, web_search_enabled, default_model_id, default_reasoning_level, owner_id, scope, can_edit.

`Conversation`: id, assistant_id, title, archived, active_run_id (nullable), created_at, updated_at, model_id, model_label, reasoning_level, has_native_session (boolean), category (string), tags (string array). The selection fields are public names, never upstream deployment identifiers; the native-session boolean exposes readiness without exposing its internal ID.

`Message`: id, conversation_id, role (`user|assistant`), text, file_ids:string[], run_id (nullable), created_at, skills (`[{id,name,revision}]` snapshots on user messages), citations (minimal registered source summaries on assistant messages).

`Run`: id, conversation_id, status (`queued|running|awaiting_approval|cancelling|succeeded|failed|cancelled|interrupted`), assistant_message_id (nullable), error (nullable safe string), usage (nullable object), approvals (pending approval objects), created_at, completed_at, model (public label), model_id, reasoning_level, provider (public service label), timings (observed stage milliseconds). Historic runs without a selection snapshot use `历史模型`, `legacy-model`, and a null reasoning_level rather than guessing a route.

`File`: id, name, mime_type, size, created_at. Download URL is `/api/v1/files/{id}/content`.

`Approval`: id, run_id, tool_name, arguments (JSON object), status, created_at, expires_at, risk_level (`low|medium|high`), approval_policy, impact, argument_digest, decided_at (nullable), decided_by (nullable), decision_source (`user|model|policy|dual_admin|timeout|cancelled|interrupted|restart|run_finished`, nullable), strategy (`user|model|dual`), approval_mode (`single|dual`), required_approvals, approver_ids, approval_policy_id and optional `review` (`model_id`, `policy_id`, `policy_revision`, `policy_digest`, `operation`, `data_scope`, `amount`, `reason`, `result`, `decided_at`). Arguments are redacted for display and remain an immutable server-side snapshot; the digest is only an audit proof and never exposes secret values. Decision approves those exact arguments only. Unknown tools use `deny_by_default`; external MCP tools are conservatively high risk. Model decisions are local synthetic evidence only and do not imply a real model endpoint is authorized. Dual decisions require two distinct enabled administrators from the immutable allowlist; the requester may deny but may never approve their own high-risk action, and a policy revision/restart/expiry invalidates the request.

`UsageSummary`: total_runs, successful_runs, input_tokens, output_tokens, estimated_cost_usd (nullable), currency (`USD`), users (array of user aggregates, admin only). When provider accounting isn't reliable, mark cost unknown rather than zero or applying Claude pricing to DeepSeek.

## Routes

| Method | Path | Request/response |
|---|---|---|
| GET | /config | `{brand_name,upload_max_bytes,auth_types:["none","bearer"],model_label,default_model_id,default_reasoning_level,feishu_h5:{enabled,app_id,path}}`; `feishu_h5.app_id` belongs to the separate workbench app and no app secret is returned |
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
| GET/POST | /conversations | Conversation[] / `{assistant_id,title?,model_id?,reasoning_level?,category?,tags?,auto_model_routing?}` → Conversation |
| GET/PATCH | /conversations/{id} | Conversation / `{title?,archived?,model_id?,reasoning_level?,category?,tags?,auto_model_routing?}` |
| GET | /conversations/{id}/messages | Message[] |
| POST | /conversations/{id}/messages | `{client_request_id,text,file_ids:[],skill_ids:[],model_id?,reasoning_level?,auto_route?}` → 202 `{message,run_id}`；缺少显式 `model_id` 时，启用会话自动路由则按公开能力进行有界本地选模 |
| GET | /runs/{id} | Run |
| GET | /runs/{id}/trace | Owner-authorized sanitized JSON: run metadata/timing/usage, linked messages and persisted product events |
| GET | /runs/{id}/events?after=0 | replayable SSE; Last-Event-ID supported |
| POST/GET | /runs/{id}/subagents | Owner-authorized local synthetic child plan create/list; request `{mode:"synthetic",tasks:[string|object]}`; no model/network/MCP mode |
| POST | /runs/{id}/subagents/cancel | Owner-authorized cancellation cascade for the synthetic child plan |
| POST | /runs/{id}/cancel | Run or `{ok:true}` |
| POST | /approvals/{id}/decision | `{decision:"allow"|"deny"}` → approval status |
| GET | /approvals/pending | Administrator-only pending dual approvals assigned to the current administrator; arguments are redacted |
| GET/PUT | /assistants/{id}/model-approval | Administrator-only local synthetic model-review policy; PUT accepts `{enabled,reviewer_mode:"synthetic",model_id,allowed_tools:[Read|Glob|Grep],allowed_data_scopes,max_amount,allowed_operations}`; `model_id` must use the `synthetic-*` namespace; default disabled and deny-first |
| GET/PUT | /assistants/{id}/high-risk-approval | Administrator-only local high-risk policy; PUT accepts `{enabled,policy_id,allowed_tools,admin_allowlist,required_approvals:2}`. Enabled policies require two distinct enabled administrators; an empty tool/administrator allowlist denies by default. |
| POST | /files | multipart field `file` → File |
| GET | /files/{id}, /files/{id}/content | metadata / authorized bytes |
| POST | /artifacts/xlsx | `{filename,sheets:[{name,columns,rows}]}` → user-owned File plus `download_path` and `preview_path`; bounded local export with formula-text escaping |
| GET | /files/{id}/xlsx-preview | bounded workbook preview (`?rows=1..200&sheets=1..10`): sheet tabs, columns, cell text, formula and escaped-text counts; formulas are never evaluated |
| GET | /artifacts/image-capability | image route status; `available` is true only for an enabled, allowlisted and reviewed `openai-images-v1` route; no credentials or private provider URL |
| POST | /artifacts/image | async `{prompt,model_id?,conversation_id?,size?}` → `{job_id,file,format,download_path,preview_path,usage,capability}`; without an accepted route it records an `unavailable` job and returns 503 `IMAGE_MODEL_UNAVAILABLE` instead of fabricating an image |
| GET | /artifacts/image-jobs?limit=1..50 | the caller's own image jobs with status, error code, file id, bytes and usage; prompts are stored only as a digest and character count |
| GET | /usage | UsageSummary |
| GET | /models/router-status | Authenticated, metadata-only readiness for the exact fitness/Tab target; never makes a provider call |
| GET | /feishu/h5/config | `{enabled,app_id,path}`; public H5 capability metadata, with no secret |
| POST | /feishu/h5/login | `{code}` from `tt.requestAccess` or `tt.requestAuthCode` → User plus the normal HttpOnly session cookie; the code is single-use and the returned open ID is allowlisted and mapped to a local member |

## Model selection and private routing

`GET /models` returns an array of `{id:string,label:string,available:boolean,unavailable_reason?:string,reasoning_levels:[{id:string,label:string}],default_reasoning_level:string,requires_new_conversation_on_switch:boolean}`. The current public catalog contains `deepseek-v4.1-flash`, `glm-5.3` / `GLM-5.3`, `dano-v2.1` / `Dano-v2.1`, `hy4` / `混元 4`, and `kimi-k3.1-pro` / `Kimi 3.1 PRO`. The last two are present in the catalog but remain unavailable until an administrator supplies a valid enabled upstream route. Dano-v2.1 is Better AI's industrial assistant product. There is always at least one reasoning option. Models with no selectable effort expose `[{id:"default",label:"默认"}]`, mapped internally to an omitted SDK effort option.

Creating a conversation without an explicit selection uses the assistant defaults first, then the deployment defaults. The conversation remains bound to this assistant; its assistant_id is not patchable. A model change without `reasoning_level` uses that model's default. A change of only `reasoning_level` retains the current model. Dano's levels are `light`, `high`, `max`, privately mapped to upstream `low`, `high`, `max`. An unknown ID returns `422 UNKNOWN_MODEL`; a known but disabled/unconfigured deployment returns `503 MODEL_UNAVAILABLE`; an unsupported level returns `422 UNSUPPORTED_REASONING_LEVEL`. Failed changes do not alter the saved selection.

GLM-5.3 currently sets `requires_new_conversation_on_switch:true`: use it in a new conversation. If a conversation has a native SDK session and a PATCH changes the public model ID, a protected old or new model returns `409 MODEL_NEW_CONVERSATION_REQUIRED` with `请新建对话使用此模型；原对话和记忆保留`. This rule is bidirectional, including historical public aliases whose private upstream route matches a protected catalog entry. It does not erase context, silently fork, or change the original model. An empty conversation without a native session may select GLM normally; subsequent turns within its GLM session remain available. Same-model reasoning changes and Dano/DeepSeek switching remain available. The client uses `has_native_session` and this public capability to explain the limitation before submitting a forbidden switch. This boundary follows observed repeated GLM continuation timeouts on cross-model history, while a fresh GLM conversation succeeded; it is not a general claim that the provider can never support such history.

When `auto_model_routing` is enabled, task-submit routing sends only the sanitized current task text and available catalog capabilities to the bounded router. If the administrator enabled a valid authorized target, one provider call is made; otherwise the local scorer is used and the target evidence remains explicit. The resulting public `model_routing` decision is recorded in private run execution metadata. An explicit one-shot `model_id` bypasses routing for that run and does not change the conversation's durable selection. Active runs are rejected before routing; routing failures, unavailable candidates, and native-session switch restrictions retain the pinned selection. Changing the conversation model through the selector disables automatic routing until `auto_model_routing:true` is set explicitly.

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
- `subagent.plan.created`: `{plan_id,mode:"synthetic",depth,max_children,children:[safe child summaries]}`.
- `subagent.started` / `subagent.progress`: `{plan_id,child_id,stage,progress?}`.
- `subagent.completed` / `subagent.failed` / `subagent.cancelled`: `{plan_id,child_id,stage}`; no task text, private history, credentials or chain-of-thought.
- `subagent.plan.completed`: `{plan_id,status:"succeeded"|"partial"|"failed"|"cancelled"|"interrupted",summary}`.

On terminal event, refetch canonical messages/conversation/run. Active run is returned when reopening a conversation. Same client_request_id returns original message/run, concurrent different request returns 409 CONVERSATION_BUSY. Startup marks in-flight runs interrupted and approvals invalid; never replay external writes automatically.

## MCP and execution constraints

- Remote Streamable HTTP MCP supports none/bearer authentication; the deployment administrator maintains allowed destination hosts. Members manage their private connections and administrators manage team connections. A separate fixture MCP service is included for deterministic acceptance.
- Test connects and discovers tools from the execution environment without executing business mutations.
- Configure connections/assistants in the client; backend injects allowed config/credentials into per-conversation SDK sessions. Cwd/settings/transcripts isolated, no global user hooks/settings loading. Import only explicit provider environment keys from an authorized server-side config file.
- No arbitrary user-supplied stdio commands or Bash/Agent tool access. Public web retrieval is a guarded product capability enabled per assistant, rather than unrestricted native WebFetch/WebSearch access. File operations remain within the current conversation workspace or the current conversation's native memory directory; other users, assistants and hidden SDK configuration are denied independently of approval.
- Runtime evaluates current user/assistant/connection/tool grants on every tool call, not only when creating the session. Hook failures deny execution. `allowed_tools` auto-approval is distinct from authorization.
- Model review is a separate strategy: only an enabled administrator policy can invoke the local synthetic reviewer for low-risk tools. The server rechecks authorization, exact argument digest and policy revision before execution; high-risk and unknown tools always use human/admin approval, while reviewer errors, timeouts, malformed/extra fields, low confidence or uncertainty deny by default.
- High-risk dual approval is opt-in per assistant in this local phase; without a configured policy the existing user-confirmation path remains visible as an incomplete legacy boundary and must not be called production delivery. When enabled, only allowlisted tools and administrators can approve; one deny terminates the request, two distinct allows are required, and restart/cancel/expiry never replays votes.
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

`GET /conversations/{id}/context` reads a persisted snapshot and makes no provider request. `POST /conversations/{id}/context/refresh` has no body and runs in the conversation's owning actor. Claude uses its public SDK `get_context_usage()`; Harness uses a session-bound, read-only `workspace/context` request to the pinned official token meter. A valid, authorized snapshot less than 60 seconds old is reused; errors and stale snapshots are not treated as successful cache hits. An active run or approval returns `409 CONTEXT_BUSY`. A conversation without a native session returns unavailable statistics without starting a model query.

Context object retains `total_tokens`, `max_tokens`, `percentage`, `auto_compact_enabled`, `auto_compact_threshold`, `model`, `measured_at`, `cached`, `error` and `last_compaction`. `source` is `native_runtime|harness_token_meter`; `status` is `available|partial|unavailable|not_measured`. Claude `available` still requires native total, limit and percentage; absent values remain null. Additional fields are `precision:native_reported|estimated|unknown`, `baseline_kind:usage|estimated|none|null`, `observed_prompt_tokens:number|null`, `observed_at:ISO|null`, `stale:boolean`, `stale_reason:route_changed|request_unobserved|snapshot_expired|null`, `projection_seq:number|null`, `manual_compact_available:boolean` and nullable `breakdown:{system_tokens,tools_tokens,message_tokens}`. Private bindings, paths, request headers, raw projections and provider identities are never returned. A new native query invalidates the old snapshot.

Harness `total_tokens` is the official `tokenMeter.measure()` estimate of current request-and-response context pressure, not accumulated billed usage or the input size of a future user message. A `usage` baseline includes normalized runtime input/cache/output buckets plus estimated context changes; it is still an estimate. The pinned adapter fills absent raw input/output buckets with zero and does not preserve their presence bits, so normalized counts cannot prove that a supplier actually reported them. This integration therefore keeps `observed_prompt_tokens` null, including normalized nonzero and zero values; it does not relabel a runtime default as a verified provider observation. Composition estimates must not be added to the displayed total or used for billing. The pinned adapter's default one-million-token window is not verified supplier capacity: this integration keeps Harness `max_tokens`, `percentage` and `auto_compact_threshold` null and reports `partial` when estimates are otherwise usable. `observed_at` remains null because this projection provides no supplier sample timestamp; `measured_at` only records the statistics read time.

Harness refresh sends no prompt, count request, tool call or model request. It may cold-start the owned process and restore the existing native session; official recovery can persist interrupted-turn closers, so only the statistics RPC is described as read-only, not the complete recovery lifecycle. The RPC has a 15-second timeout, separate from existing startup/resume deadlines; its process is closed after the operation. Current configuration, authorization, native-session identity and a host-recorded successful-run binding must match before statistics are returned or reused. Unknown legacy bindings, changed routes/configuration and invalidated snapshots keep quantities unknown instead of generating a new request to fill them in. Product-metadata-only answers do not invalidate an otherwise unchanged native history. The locked Harness profile keeps automatic compaction disabled; the controlled manual-compaction contract is available below.

Claude context measurement can make additional provider token-counting requests; it is not claimed to be local or free. Both clients should refresh explicitly, not on each SSE event. Measurement errors produce safe unavailable objects; they do not fail an otherwise successful chat or compaction. The client must distinguish estimated, unknown and stale values and must not enable compression when runtime capability is unknown.

### Native manual compaction

For Claude and Harness conversations, `POST /conversations/{id}/compact` accepts `{client_request_id:string,instructions?:string}` and returns `202 {message,run_id}`. It runs through the same session actor; there is no second application summarizer. Claude dispatches its native `/compact`. Harness invokes a product-owned `/compact` command over the pinned official compaction engine without modifying upstream; optional user focus is appended to the summary request as clearly delimited untrusted data that narrows what to preserve but cannot change the output structure or grant tools. No native session yields `409 CONTEXT_NOT_READY`; execution/approval conflicts use the existing busy errors. Repeating a request ID returns its original run.

Harness success requires correlated `command/run`, `command/done`, `compaction/start`, `compaction/summary` and `compaction/end` evidence for the same owned session, command and compaction transaction. A summary sequence is required for `completed`; a successful command without a compaction boundary is `not_needed`, never a claimed compression. Summary input/output/cache usage is added incrementally to the run ledger with cost unknown unless the supplier actually reports a reliable cost. Timeout, cancellation, protocol failure or command failure closes the owned executor and reaps its request thread. If a real boundary already occurred, compaction remains `completed` and `followup_status` records failure of later steps. `manual_compact_available` is true only for an idle, authorized native session whose current user, assistant and configuration can still be verified; the endpoint repeats the full check and does not trust this hint as authorization.

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
| POST | /knowledge-bases/{id}/search | `{query,limit?,mode?,context_chars?}` → `{results,retrieval,context}`; `mode` is `hybrid`, `bm25`, or `vector` |
| GET | /knowledge-bases/{id}/graph?q=&limit= | Current ready document-version graph with source-linked nodes and edges; authorized knowledge-base access is rechecked |
| GET | /knowledge-documents/{id}/image?version=N | Authorized image metadata, bounded thumbnail, grid summary and region previews; visual/entity status remains `needs_model` without an approved model |
| GET | /knowledge-documents/{id}/regions?version=N&limit=16 | Authorized, immutable-version-bound grid regions with source pixel coordinates and safe preview URLs; at most 16 regions |
| GET | /knowledge-documents/{id}/regions/{region_id}/thumbnail?version=N | Permission-checked JPEG preview for one version-bound region |

KnowledgeBase fields include `id,name,description,scope,owner_id,can_edit,enabled,revision,default_for_new_assistants,document_count,index_status,retrieval`. Only administrators may mark an enabled team knowledge base as a default. `index_status` is a count map by status, not a fabricated completion percentage.

Document metadata includes `id,kb_id,name,mime_type,version,sha256,size,status,error,page_count,chunk_count,embedding_model,extraction,created_at,content_url`; image documents additionally expose `image.region_count`, `image.regions_url` and `image.grid`. Status is `processing`, `ready`, `needs_ocr`, or `failed`. A failed or empty extraction is not ready. Originals and prior versions are retained separately; uploads keep the document format. File sizes follow deployment upload limits; the parser also bounds PDF pages and extracted text. Physical page numbers apply only to PDFs. Text/DOCX content uses sections, paragraph indices and extraction offsets. Grid regions are layout previews only, not semantic segmentation or visual entities.

Search result fields include `document_id,version,name,title,text,excerpt,page,section,paragraph,start,end,chunk_id,score,rank,lexical_score,vector_score,retrieval_mode` and knowledge-base metadata. Image and PDF-page hits may additionally include `media` with `kind`, source `sha256`, version-bound `thumbnail_url`, dimensions/page, bounded region previews, and `visual_index_status`; this is evidence attachment after text ranking, not visual-vector recall. Retrieval reports `mode`, `available_modes`, `lexical`, `vector_model`, `semantic` and `execution`; `context` is a bounded copyable RAG preview with `text`, `characters` and `truncated`. BM25 can be selected directly or combined with the configured local vector method; `local-lexical-v1` is explicitly nonsemantic. Run admission pins visible document versions; retrieval and source reopening also recheck current authorization. The application supplies native targeted-read and retrieval tools through the same guarded runtime. See [knowledge implementation and deployment](KNOWLEDGE_AND_SOURCES.md).

### Registered evidence and references

`GET /runs/{id}/sources` returns an array of authorized source details; `GET /sources/{id}` returns one authorized source. Source kinds are `data`, `knowledge`, and `web`. Detailed fields include `id,kind,title,locator,excerpt,created_at,url,original_url?,document_id?,document_version?,content_url?`. The locator is structured actual provenance, with public logical names instead of server filesystem paths or credentials.

Data locators include available database/schema/table/fields/records/filters/time range/timezone/query time/aggregation; missing fields are marked missing. Knowledge locators identify original file/hash/version and a real PDF page or extracted paragraph. Web locators identify original URL, fetch time, excerpt/content type, and publication time only when observed. Chat attachments register their own file content URL and ownership, rather than pretending to be a knowledge-base document.

Only actual tool results register opaque source IDs. The model references `[[source:ID]]`; finalization validates it and creates numbered Markdown links plus a canonical footer. Unrelated conversation IDs and model-authored raw internal source links cannot create references. Historical sources from the same conversation are reauthorized before reuse. Persisted message citations keep minimal `id,number,kind,title,url,document_id?,document_version?`; sensitive locator/excerpt details are fetched from the source endpoint on demand.

The client makes internal source controls clickable only when their canonical ID is registered in the current message citations. Unverified or encoded internal source links remain inert. Source access checks the run owner and current resource/assistant/tool permission. Original answer text remains historical conversation content after revocation, while source details and original bytes are denied. PDF original links include the saved version and optional `#page=N`. Attachment links are restricted to `/api/v1/files/{id}/content`.

### Public web capability and timing

`Assistant.web_search_enabled` controls guarded search and page reading. The current search provider is Bing RSS; page fetches validate public DNS and redirects, bound content/time, reject private/local addresses, and accept text content only. The runtime registers search snippets and fetched page text as distinct evidence. Search-provider availability is separate from model availability. No API key for web search is exposed to the client.

`Run.timings` and `timing.updated` use elapsed milliseconds from run admission for runtime readiness, first model event, first visible text and total completion. They measure observable client/server stages; they are not upstream queue-duration measurements. Tool event timing is retained with the actual operation. Missing historical timings remain unknown. Canonical `message.updated` must replace streamed draft text, and terminal clients refetch messages/run as usual.

### Advisory model fitness recommendation

`POST /api/v1/models/recommend` accepts `{ "task_summary": string, "candidate_model_ids"?: string[] }`. The summary is capped at 2,000 characters and candidates at 20 public catalog IDs. The response includes `{model_id,model_label,reasoning_level,reason,applied:false,router}`. The operation is advisory metadata and does not itself patch a conversation, replace an active run, create a native session, or add a run/usage row. The client applies the returned selection only while the conversation is idle and then enables `auto_model_routing`; ordinary model-switch guards remain authoritative. Unavailable or unknown candidate IDs are filtered; if none remain the server returns `NO_AVAILABLE_MODELS`. `POST /api/v1/models/complete` accepts only a 500-character draft and returns a real provider completion on explicit Tab when the configured router target is available; otherwise it returns `suggestion:null` with `router.target_status=unavailable` rather than a hard-coded phrase.

The router identity defaults to the exact public `deepseek-v4.1-flash-1007` and can be overridden with `AGENT_MODEL_ROUTER_MODEL_ID` only for an explicitly authorized catalog entry. A missing exact target is reported as an evidence gap and fails closed rather than silently calling a different model; no unavailable model is fabricated. The default bounded local scorer makes no provider call. With explicit administrator configuration, one provider call receives only the sanitized task summary and public catalog capabilities, with bounded timeout/output/budget and strict response validation; failures fall back to the local scorer. `router.provider_configured` and `router.provider_status` distinguish a selectable catalog route from an actually authorized provider; missing credentials yield `provider_attempted:false`, while rejected provider output yields the bounded `provider_response_rejected` reason. Provider credentials, private upstream model names, conversation history, attachments, assistant instructions and MCP data are never sent to the router. Applying a suggestion still uses normal conversation model-switch and new-conversation guards.

`GET /api/v1/models/router-status` is an authenticated metadata-only preflight. It
returns `schema_version=model-router-preflight/v1`, the exact public target ID,
catalog availability, router enablement, positive budget state, provider
authorization state, and a `tab_completion_mode`/`ready` decision. It never
contacts a model provider, returns a private upstream model name, or exposes a
credential. `ready=true` means the provider route is eligible for a future
bounded call; it is not evidence that a paid request has succeeded.

### Versioned local workflow contract (priority 4A)

The first low-code workflow slice is a declarative, synthetic-only contract. It
does not execute a model, an MCP connection, a customer system or an artifact
writer.

| Method | Path | Input / result |
| --- | --- | --- |
| GET | `/workflow-templates` | List versioned synthetic finance templates and their safe graphs |
| GET/POST | `/assistants/{assistant_id}/workflows` | List or create a workflow with `name`, `description` and `graph:{nodes,edges}` |
| POST | `/assistants/{assistant_id}/workflows/from-template` | `{template_id,name?}` copies a synthetic template into a new draft |
| GET | `/workflows/{id}` | Workflow metadata and version summaries/graphs |
| PATCH | `/workflows/{id}/draft` | Replace a draft graph; optional `expected_revision` prevents lost updates |
| POST | `/workflows/{id}/publish` | Publish the immutable draft version and its graph hash |
| POST | `/workflows/{id}/rollback` | `{version}` creates a new unpublished draft from an earlier version |
| POST | `/workflows/{id}/runs` | `{input,conversation_id?}` runs the published version through the local synthetic replay; an optional conversation must belong to the same assistant |
| GET | `/workflow-runs/{id}` | Safe node trace, synthetic output and immutable permission snapshot; input values are never echoed |

Allowed node types are `agent`, `skill`, `knowledge_search`, `mcp_read`,
`condition`, `approval` and `artifact`. Graph validation rejects duplicate
nodes, invalid edges, cycles, isolated nodes, sensitive configuration keys,
oversized payloads and any execution mode other than `synthetic`. A run pins
the published version, records a node order and marks every step
`external_call:false`. An optional session binding is checked against the
workflow assistant and user, then persisted with a permission digest and
sanitized permission snapshot. This remains a synthetic run contract, not
customer runtime execution or a production visual editor.

### Chat-bound workflow runs and artifacts (OC-T9)

A published workflow version can now be started from inside an existing
conversation. The versioned workflow run stays authoritative; the chat only
carries a normal user message and a normal assistant message that are linked to
it, so history, reload and reuse behave like any other turn.

| Method | Path | Input / result |
| --- | --- | --- |
| POST | `/conversations/{conversation_id}/workflows/{workflow_id}/runs` | `{client_request_id,input}` → `{message,assistant_message,run_id,run}`. Requires a conversation the caller owns, not archived, whose assistant owns the workflow and whose published version exists. `client_request_id` (8–100 chars) makes a retry return the original run instead of starting a second one. An active chat run or another pending workflow run in the same conversation is rejected with 409. |
| GET | `/conversations/{conversation_id}/workflow-runs` | Recent chat-bound runs for this conversation, newest first, with `workflow_name` and the `chat` message link |
| POST | `/workflow-runs/{run_id}/cancel` | Cancels a run that is waiting for approval; marks the run, trace and pending approvals `cancelled` and rewrites the linked assistant message. Returns 409 for any other state. |

Behaviour:

- The assistant message text is derived server-side from the run state
  (`awaiting_approval` / `succeeded` / `cancelled` / `failed`), bounded in size
  and never includes raw input values.
- Approval decisions continue to use
  `POST /workflow-runs/{id}/approvals/{node_id}/decision`; the linked message is
  updated with the new state after the decision.
- For chat-bound runs, every `artifact` node with `format=xlsx` writes a real
  workbook into the conversation file area, registers a `files` row scoped to
  the conversation and adds the file id to the assistant message's attachments.
  The run output keeps the structured preview plus `written`, `file_id`,
  `file_name`, `size`, `mime_type`, `download_path` and `preview_path`. A write
  failure is recorded in `output.artifacts[].error` and does not mark a
  successful run as failed.
- Standalone `POST /workflows/{id}/runs` keeps returning structured previews
  only; it does not attach artifacts to any conversation.

## 2026-11-10 HippoRAG 官方后端的开发注入切片

`POST /api/v1/knowledge-bases/{kb_id}/search` 的请求字段保持不变。`mode=hipporag` 默认仍使用 `hipporag2-lite`；只在可信开发调用方显式注入预构建官方快照后尝试官方检索，没有客户端启用参数，也没有启动时自动安装模型。

新增可选响应字段：

- `retrieval.algorithm`：`hipporag2-official` 或既有 `hipporag2-lite`。
- `retrieval.backend`：`official` 或 `lite`；不能把回退结果标为官方成功。
- `retrieval.fallback`：受限错误代码，例如 `identity_mismatch`、`index_not_ready`、`index_untrusted`、`result_invalid`、`retrieval_failed`、`no_results`、`no_text_chunks`。不包含异常原文。
- 官方 `graph_trace` 只公开 `algorithm` 和有界的来源数、去重文本数、返回来源数、候选图种子数量；不是已核验关系或路径。
- 官方命中保留现有 `chunk_id/document_id/version/sha256/content_url/media` 等本地证据字段，`retrieval_algorithm=hipporag2-official`，不伪造归一化 `graph_score`。分数允许有限的零值与负值；来源数量受 `limit` 限制。

官方索引必须与整个已授权片段集合匹配。查询期间账号停用、权限撤销、文档删除或版本变化，返回现有权限错误或 `409 KNOWLEDGE_SNAPSHOT_CHANGED`，不把旧结果组装为上下文。运行快照允许已固定的历史版本，但禁止采用新版本。

聊天知识工具仅在相同的显式开发注入下尝试官方后端，引用仍经现有来源登记。默认聊天继续混合检索。完整限制及测试见 `design/HIPPORAG_ADAPTER.md`、`evidence/HIPPORAG_OFFICIAL_2026-11-10.md`；本合同不表示官方后端已经上线。
