# Token Savings Mainline Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Reduce real provider spend for the 知行工作台 while preserving answer quality, evidence citations, permissions, memory isolation, and auditable usage accounting.

**Architecture:** Use a staged policy. First measure provider-reported input, output, cache, compression, retry, and routing usage without treating local estimates as billing. Then reduce deterministic payloads: duplicate prompts, oversized tool results, unnecessary retrieval context, and excessive output budgets. Only after those measurements pass will the Harness automatic-compaction path or an isolated billion-context lane be evaluated for long sessions.

**Tech Stack:** FastAPI/Python 3.10, SQLite, DeepSeek Harness with the pinned Node runtime, React/TypeScript client, existing `UsageLedger`, knowledge retrieval, memory, and context contracts.

---

### 工作台短任务与分工的用量对照（2026-11-23）

沿用普通成员账号和相同模型，分别保留简单问答、多项独立分析、前后依赖任务三种样例。比较时分开记录主回答、分析子任务和后续建议的模型调用次数及输入、输出、缓存用量。除用量外，核对原问题的计算结果和各项交付是否完整。模型是否分工由普通业务提问验证，题目里不提示它调用工具。

本轮先把折叠的“继续了解”改为用户展开后生成，子任务回传保留结论、顺序及失败原因。后续优先用现有用量记录按模型观察简单问答的总消耗、每条有用建议的消耗，再决定是否调整工具说明或压缩长会话。单题的节省只记录为该样例的结果。

### Task 1: Freeze the accounting contract

**Files:**
- Create: `server/app/token_savings.py`
- Create: `server/tests/test_token_savings.py`
- Modify: `server/app/plugin_usage.py:1-185` only after the new unit contract is proven
- Test: `server/tests/test_harness_usage_ledger_plugin.py`

**Step 1: Write the failing test**

Add cases for complete, partially reported, and missing provider usage. The result must keep unknown fields unknown, separate cache-read and cache-write tokens, and expose compression, routing, retry, and answer as distinct stages.

**Step 2: Run it to verify it fails**

Run: `PYTHONPATH=server .venv/bin/python -m pytest -q server/tests/test_token_savings.py`

Expected: FAIL because the module and stage projection do not exist.

**Step 3: Write the minimal implementation**

Create a serializable `TokenStage`, `TokenAccounting`, and `SavingsObservation` boundary. Do not calculate a dollar amount when provider pricing or usage is unknown. Add a cache-hit ratio only when the denominator is known and positive.

**Step 4: Run it to verify it passes**

Run the same command and the existing ledger test. Expected: PASS with unknown values preserved.

**Step 5: Commit**

```bash
git add server/app/token_savings.py server/tests/test_token_savings.py server/app/plugin_usage.py server/tests/test_harness_usage_ledger_plugin.py
git commit -m "feat: define auditable token savings accounting"
```

### Task 2: Add a read-only usage report

**Files:**
- Create: `scripts/token_savings_report.py`
- Create: `server/tests/test_token_savings_report.py`
- Modify: `server/app/service.py:849-856` only if the report needs a shared aggregate helper

**Step 1: Write the failing test**

Feed the report synthetic run records containing answer usage, compression usage, cache usage, and missing values. Assert that it reports total known subtotals, unknown fields, stage counts, and no fabricated costs.

**Step 2: Run it to verify it fails**

Run: `PYTHONPATH=server .venv/bin/python -m pytest -q server/tests/test_token_savings_report.py`

Expected: FAIL because the report command does not exist.

**Step 3: Write the minimal implementation**

Accept a read-only JSON export or database path, aggregate by user, assistant, model, runtime, and stage, and emit JSON. Never send a model request and never alter the database.

**Step 4: Run it to verify it passes**

Run the unit test and a fixture command that writes its output under `/tmp`.

**Step 5: Commit**

```bash
git add scripts/token_savings_report.py server/tests/test_token_savings_report.py
git commit -m "feat: add read-only token savings report"
```

### Task 3: Bound deterministic payloads before compression

**Files:**
- Create: `server/app/payload_budget.py`
- Create: `server/tests/test_payload_budget.py`
- Modify: `server/app/harness_runtime.py:383-391` to apply only validated user-message and selected-skill bounds
- Modify: `server/app/runtime.py:326-330` for the Claude-compatible path

**Step 1: Write the failing test**

Test that normal messages remain byte-for-byte unchanged, selected skills are bounded by a declared budget, and truncation produces a visible marker with a digest/reference rather than silently dropping content.

**Step 2: Run it to verify it fails**

Run: `PYTHONPATH=server .venv/bin/python -m pytest -q server/tests/test_payload_budget.py`

Expected: FAIL because the policy is absent.

**Step 3: Write the minimal implementation**

Add explicit budget objects for user text, skill snapshots, and attachment references. Keep the default disabled until a deployment setting enables it. Preserve permission and source identifiers; never truncate credentials, approval payloads, or source locators silently.

**Step 4: Run it to verify it passes**

Run the focused test and the existing Harness/Claude prompt contract tests. Expected: PASS with the default policy producing no behavior change.

**Step 5: Commit**

```bash
git add server/app/payload_budget.py server/tests/test_payload_budget.py server/app/harness_runtime.py server/app/runtime.py
git commit -m "feat: add opt-in deterministic payload budgets"
```

### Task 4: Reduce tool and retrieval payloads safely

**Files:**
- Create: `server/app/result_budget.py`
- Create: `server/tests/test_result_budget.py`
- Modify: `server/app/harness_bridge.py` and `server/app/knowledge.py` only at the result-shaping boundary

**Step 1: Write the failing test**

Cover large tabular results, repeated records, empty fields, source citations, and permission metadata. Assert that the model receives bounded summaries while the full authorized result remains available by reference.

**Step 2: Run it to verify it fails**

Run: `PYTHONPATH=server .venv/bin/python -m pytest -q server/tests/test_result_budget.py`

Expected: FAIL because bounded result shaping is absent.

**Step 3: Write the minimal implementation**

Deduplicate exact rows, cap characters and rows, preserve source identifiers, and add a continuation/reference token. Do not apply this policy to writes, approvals, or data whose semantics cannot be reconstructed from a reference.

**Step 4: Run it to verify it passes**

Run the focused result tests, knowledge citation tests, MCP bridge tests, and existing source-permission tests. Expected: PASS.

**Step 5: Commit**

```bash
git add server/app/result_budget.py server/tests/test_result_budget.py server/app/harness_bridge.py server/app/knowledge.py
git commit -m "feat: bound tool and retrieval payloads"
```

### Task 5: Add task-aware output budgets

**Files:**
- Create: `server/app/output_budget.py`
- Create: `server/tests/test_output_budget.py`
- Modify: `server/app/config.py`, `server/app/harness_runtime.py`, and `server/app/model_recommendation.py`

**Step 1: Write the failing test**

Test default, short-answer, structured-output, and long-analysis budgets. Verify that a lower budget is never applied to an approval, file, or artifact task without an explicit contract.

**Step 2: Run it to verify it fails**

Run: `PYTHONPATH=server .venv/bin/python -m pytest -q server/tests/test_output_budget.py`

Expected: FAIL because task-aware output selection is absent.

**Step 3: Write the minimal implementation**

Keep the existing global maximum as the hard ceiling. Add an opt-in task policy with conservative defaults and an audit field recording the selected budget and reason.

**Step 4: Run it to verify it passes**

Run focused tests, configuration tests, and Harness real-runtime contract tests. Expected: PASS.

**Step 5: Commit**

```bash
git add server/app/output_budget.py server/tests/test_output_budget.py server/app/config.py server/app/harness_runtime.py server/app/model_recommendation.py
git commit -m "feat: add auditable task-aware output budgets"
```

### Task 6: Evaluate automatic context management

**Files:**
- Create: `scripts/token_savings_ablation.py`
- Create: `server/tests/test_token_savings_ablation.py`
- Modify: `docs/evidence/TOKEN_ACCOUNTING_2026-11-08.md` after real evidence exists

**Step 1: Write the failing test**

Define three arms: no compression, current Harness compaction, and isolated billion-context. Require equal tasks, model route, tool results, and output limits. Assert that compression calls are included in total provider usage and unknown provider fields remain unknown.

**Step 2: Run it to verify it fails**

Run: `PYTHONPATH=server .venv/bin/python -m pytest -q server/tests/test_token_savings_ablation.py`

Expected: FAIL because no real ablation runner exists.

**Step 3: Write the minimal implementation**

Use only synthetic industrial conversations first. Keep billion-context disabled by default and run it only in a separate profile with per-session storage, loopback management endpoints, rollback, cancellation, and source/fact retention checks.

**Step 4: Run it to verify it passes**

Run the synthetic ablation and the fixed Harness runtime tests. Real provider runs require an explicitly authorized TokenHub route and must be reported separately.

**Step 5: Commit**

```bash
git add scripts/token_savings_ablation.py server/tests/test_token_savings_ablation.py docs/evidence/TOKEN_ACCOUNTING_2026-11-08.md
git commit -m "test: compare token savings strategies"
```

### Task 7: Release gate

Do not enable automatic compression globally until all of the following are true:

- Total provider usage includes answer, compression, routing, retry, and cache stages.
- The long-task median input reduction is at least 20 percent without a critical evidence, permission, or memory error.
- Short tasks do not become more expensive on median total provider usage.
- No unknown provider value is converted to zero.
- Cross-user/session isolation, cancellation, restart, and rollback pass on the school-cloud profile.
- Product documentation records the active policy, limits, evidence source, and rollback command.

The first production-safe default should be deterministic payload budgeting plus accurate measurement. Automatic Harness compaction and billion-context remain opt-in until this gate passes.
