# Autonomous sub-agent orchestration design

**Status:** research/design only; no production deployment or feature flag change is included.

**Review date:** 2026-11-05

## Scope and evidence boundary

This note answers whether and how the current server could add autonomous child agents. It is based on a read-only inspection of the local source and contracts, plus the primary sources linked below. It does not claim that the pinned `claude_agent_sdk` version in this checkout supports every current Agent SDK or Managed Agents feature. Provider billing, real customer MCP behavior, load limits, and cross-process recovery have not been qualified.

The proposed controls are design requirements. They are not evidence that a child-agent implementation already exists.

## Current runtime boundary

The present runtime has useful isolation primitives, but it is a single-conversation executor:

| Area | Observed behavior | Consequence for children |
| --- | --- | --- |
| Session ownership | `RunManager` keeps one `SessionActor` per conversation. The actor serializes its queue and owns one `SDKRuntime`/SDK client. `max_active_sessions` defaults to 4. | A child cannot share the parent actor or SDK client. Parallel children need separate actors or a scheduler above them. |
| Workspace | The SDK runtime uses `data_dir/workspaces/<conversation_id>`, a private SDK config directory, and a separate native memory directory. Host `AGENTS.md`/`CLAUDE.md` instructions are excluded. | Every child needs a distinct conversation/workspace/config root and a fresh provider/session identity. A shared cwd would break the current security assumption. |
| Memory | Native memory is per user and conversation, with Markdown-only paths, revision checks, a 100-file limit, 128 KiB per-file and 1 MiB total limits. `MEMORY_BUSY`/`CONVERSATION_BUSY` protect edits while a run is active. | Default child memory must be isolated and ephemeral. Parent facts should be handed off as bounded, untrusted input or explicit artifacts; never silently mount the parent's memory directory. |
| Authorization | `PreToolUse` and `can_use_tool` re-check current user/assistant/connection grants on every call. `allowed_tools` is only an auto-approval hint. Bash, Agent/Task, Web, Skill and other high-risk tools are currently disallowed. | Child authority must be a narrower capability snapshot, revalidated at execution time. Enabling Agent/Task alone would not establish product authorization. |
| Approvals | A pending approval is tied to a run and user, expires after `AGENT_APPROVAL_SECONDS` (default 600 s), redacts arguments, and resumes only after the decision. Restart expires pending approvals. | Child approvals need child run IDs and parent-visible notices. A parent cancel or restart must deny/expire every descendant approval. |
| Cancellation/restart | Cancellation interrupts the exact actor and marks the run cancelling/cancelled. Startup marks all non-terminal runs interrupted and never replays them; external writes are not automatically retried. | Cancellation must cascade down the parent-child tree and wait for child cleanup. Recovery must leave accepted-but-unfinished child work interrupted, not replay it. |
| Usage/budget | Each SDK client receives `max_turns` (default 30) and `max_budget_usd` (default 5). Usage is persisted per run; estimated cost is reported only when provider accounting is marked reliable, otherwise cost is `null`. | Per-child SDK caps are not a parent cap. A scheduler must reserve and enforce a hierarchical budget before admitting children and treat unknown provider cost as unknown, never zero. |
| Persistence/events | Runs and approvals are SQLite rows. Events are persisted before SSE emission and replayed by run. `finish()` finalizes citations and emits `run.completed`. | Child lifecycle events need stable parent/child IDs and terminal states, while preserving replay and redaction. |

Evidence for these observations: [runtime actor and SDK options](../../server/app/runtime.py), [RunManager recovery/capacity/cancellation](../../server/app/runtime.py), [service run preparation and usage aggregation](../../server/app/service.py), [memory scope and quotas](../../server/app/memory.py), [database schema/migrations](../../server/app/db.py), [public API and security contract](../API_CONTRACT.md), and [settings defaults](../../server/app/config.py).

## What the primary sources say

The official Claude material describes three distinct patterns:

1. **SDK Agent/Task delegation.** The orchestrator delegates to a child with its own context, tools and expertise, then receives the result while retaining control. The cookbook says to add `Agent` to the allowed tools and define programmatic `AgentDefinition` entries (the older name was `Task`). See [The chief of staff agent](https://platform.claude.com/cookbook/claude-agent-sdk-01-the-chief-of-staff-agent) and [the SDK migration note](https://platform.claude.com/cookbook/claude-agent-sdk-04-migrating-from-openai-agents-sdk).
2. **Parallel tool calls.** The Messages API may return several tool calls in one assistant turn. The application decides whether to run them concurrently or sequentially; independent read-only work is usually suitable for concurrency, while side effects and ordering should be serialized. Every call still requires a matching result, including an error result for skipped work. See [Parallel tool use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/parallel-tool-use).
3. **Dynamic workflows.** The Agent SDK cookbook's workflow primitives make the plan a program: `agent()` starts clean-context workers, `parallel()` is a barrier, and `pipeline()` streams items through stages. The cookbook warns that model-decided subagents keep the plan only in model context and do not guarantee complete aggregation or verification; workflows are more suitable when structural verification or durable orchestration matters. See [Dynamic workflows](https://platform.claude.com/cookbook/claude-agent-sdk-08-dynamic-workflows).

Managed Agents documentation adds useful limits and trade-offs: subagents have isolated session threads; dynamic workflows run many agents in phases; a session budget caps spend across runs; and the documented managed-agent limits include a maximum of 25 child threads at a time and one-level delegation for that product surface. Those limits are **not evidence that the local Python SDK enforces the same values**. See [Multiagent orchestration](https://platform.claude.com/docs/en/managed-agents/multiagent-orchestration), [session budgets](https://platform.claude.com/docs/en/managed-agents/budgets), and [prompting guidance on spawn caps](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-opus-5).

DeepSeek's open-source Harness provides a useful provider-neutral comparison. Its [subagent package](https://github.com/deepseek-ai/deepseek-harness/tree/master/packages/subagent) separates fresh in-process children, history-seeded forks, and out-of-process ACP/Codex/Claude Code/DSH SDK providers. The [subagent subsystem contract](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/subagent.md) gives each activation a stable child identity, parent ownership, capacity admission, cancellation and disposal. Child-level failures resolve as structured stop reasons (`completed`, `aborted`, `error`, `max-tokens`, `refusal`) rather than being confused with infrastructure exceptions; cleanup is awaited before final settlement. The package also warns that process-local residency does not coordinate two Harness processes and that accepted-but-unlogged messages are not replayed automatically. These are design patterns from an open-source project, not claims about this repository.

For memory, the independent [dsh-memory plugin](https://github.com/zhbcher/dsh-memory) demonstrates user/project scopes, byte budgets, source-session provenance, archived entries, lexical retrieval, and bounded auto-distillation at idle, disposal and compaction. Its own limitations say retrieval is lexical rather than embedding-based and session navigation is not integrated. This supports provenance and bounded injection as useful controls; it does not justify adding an automatic summarizer to this product.

## Architecture comparison

| Option | Decomposition and parallelism | Isolation and memory | Failure/approval/cost controls | Fit here |
| --- | --- | --- | --- | --- |
| **Native SDK Agent/Task** | Model chooses when to delegate; parent remains the orchestrator. Children get separate context and can run in parallel where the SDK/provider supports it. | Agent definitions can have their own model/tools/MCP, but exact workspace, memory and permission inheritance must be verified for the pinned SDK. | SDK `max_budget_usd` is a per-client ceiling. Product still needs parent budget, child count/depth, approval routing, restart handling and audit rows. | Good later behind a feature flag. Current runtime explicitly disallows `Agent`/`Task`; enabling it before policy and persistence work would bypass product controls. |
| **Application-owned bounded fan-out (recommended first)** | Server creates a finite plan, starts independent child runs, uses a barrier, then gives a bounded result set to the parent. Read-only calls can use `asyncio.gather`; writes are sequential. | Each child receives a new internal run/session ID, workspace, SDK config, memory scope and capability snapshot. Parent handoff is explicit and size-limited. | Scheduler owns admission, hierarchical budgets, depth/child caps, cancellation cascade, approval mapping, idempotency and terminal aggregation. | Best match for the existing SQLite/event/actor model and reviewability. It does not require trusting a model to create arbitrary children. |
| **Dynamic workflow/DAG** | Program declares phases, barriers and pipelines; retries and aggregation are deterministic and can continue independently of the parent turn. | Workflow state and artifacts need durable storage. Each node still needs child session/workspace/memory isolation. | Strongest place for deadlines, retries, quorum/partial-result policy and budget reservation, but more schema and scheduler work. | Phase 3+ after bounded fan-out proves the contracts. |
| **DeepSeek Harness provider adapter** | Provider registry can run fresh, forked, ACP or external DSH children. Stable child IDs, lifecycle handles and structured stop reasons are good interoperability targets. | Supports in-process and out-of-process children, but cross-process persistence requires a durable mailbox and leases. Memory plugin offers provenance and budgets. | Manager-level capacity, parent ownership, cancellation and disposal are explicit. Product approvals and credential scopes still remain host responsibilities. | Useful as an adapter boundary or reference, not a drop-in replacement for this Python runtime. |

## Recommended contract

Start with application-owned orchestration and keep native Agent/Task disabled. Introduce a child task record linked to the existing parent `runs` row. At minimum it should retain:

- stable `child_id`, `parent_run_id`, `root_run_id`, user and assistant ownership, depth and role;
- immutable model/provider digest, capability snapshot and tool policy;
- separate workspace/config/memory scope identifiers (never the parent's paths or SDK session ID);
- status (`queued`, `running`, `awaiting_approval`, `succeeded`, `failed`, `cancelled`, `interrupted`) plus a provider-neutral stop reason;
- reserved and observed budget, turn/token/time limits, and redacted diagnostic text;
- parent delivery mode (`result` or bounded event), output schema/version, artifact references and provenance;
- creation, completion and cleanup timestamps, retry count and idempotency key.

The scheduler should admit a child only after checking all of the following: parent is still active; user/assistant is authorized; depth and child-count limits are below policy; a budget reservation succeeds; the requested tools are a subset of the parent’s capability snapshot; and the child workspace/memory roots are newly allocated. The child receives a task prompt plus explicit data/artifact references. It does not receive the entire parent transcript or private memory by default.

Use a structured child result with `output`, optional validated `structured` data, bounded `diagnostic`, `stop_reason`, usage and artifact/source IDs. Treat model/provider failures as child results that the parent can classify; reserve thrown exceptions for scheduler/infrastructure failures. Always await child disposal before releasing capacity. This mirrors the strongest parts of the DeepSeek contract while fitting existing event replay.

Aggregation must be declared before fan-out. For independent research, the default is “collect all successful results, report failed children separately, and let the parent decide whether the evidence is sufficient.” For write operations, require a parent review/approval barrier and an idempotency key; never let a child retry a side effect automatically after restart. A parent cancellation marks descendants cancelling, denies pending approvals, interrupts each actor, waits for cleanup, then emits one terminal parent event.

Memory handoff should be explicit and provenance-bearing. A parent may pass a bounded snapshot or a list of artifact/source IDs; a child may return a proposed memory update, but applying it requires the parent policy and the existing revision check. Do not mount the parent’s native memory directory or allow child-written Markdown to become shared instructions. If durable child memory is later needed, model it as a separate scope with byte budgets, source child IDs and a user-visible retention policy.

## Phased implementation and acceptance

1. **Contract freeze (no deployment).** Add schema/API design for parent-child IDs, stop reasons, budgets, artifact handoff and event vocabulary. Acceptance: source review confirms no parent SDK session, workspace, memory or approval row is reused; current tests remain unchanged.
2. **Single child, sequential and read-only.** Build an internal scheduler that launches one child with a fresh actor/workspace/memory scope and returns a structured result to a parent test run. Disable writes and Agent/Task. Acceptance: child can read only the authorized synthetic MCP/data; parent and child transcripts, memory files, provider digests and events remain separate; a child failure does not fail cleanup.
3. **Bounded parallel fan-out.** Add a semaphore below `max_active_sessions`, explicit barrier and per-child budget reservations. Permit parallel read-only work; serialize any tool marked side-effecting. Acceptance: two children finish independently while one is awaiting approval or is cancelled; capacity, usage and SSE replay remain correct; one child failure does not leak or cancel siblings.
4. **Approval and recovery hardening.** Route child approvals to the parent UI with child/run IDs and redacted arguments. Add cancellation cascade, timeout, idempotency and restart recovery. Acceptance: parent cancellation expires all descendant approvals; restart marks descendants interrupted with no replay; denied/expired approval cannot execute a later write.
5. **Durable workflows.** Add optional persisted DAG phases, retries only for declared idempotent reads, quorum/partial-result policies, artifact provenance and trace export. Acceptance: a workflow can resume its bookkeeping after process restart without replaying accepted external side effects; every cost, token and unknown-provider-cost state is attributable to a child.
6. **Optional native SDK adapter.** Inspect the pinned SDK release and add a feature-flagged Agent/Task provider only after steps 1–5 pass. Start with one predefined read-only agent and deterministic spawn/depth/concurrency caps; compare its events and cleanup with the application-owned path. Acceptance: disabling the flag removes the tool from the runtime; native delegation cannot bypass the same capability, approval, workspace, memory, budget and audit checks.

## Explicit non-goals and unresolved questions

- No production deployment, model catalog change, or permission broadening is made by this note.
- The local checkout does not establish whether the installed SDK exposes current `AgentDefinition`, workflow, child-spawn limits, or child event APIs; inspect the exact package version before implementation.
- Provider cost may remain unknown for DeepSeek/other routes. A child budget reservation can bound requested work, but it cannot manufacture provider billing data.
- Cross-process durable children, distributed leases, and remote MCP side-effect semantics need dedicated acceptance tests. The current process-local actor map is not a distributed coordinator.
- Native model “parallel tool use” is different from parallel child agents. Tool-call concurrency must still obey per-tool authorization, side-effect ordering and approval policy.

