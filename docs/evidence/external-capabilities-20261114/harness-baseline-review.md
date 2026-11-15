# Harness baseline: file-tool exact-set failures

## Scope and conclusion

- Read-only audit of `/media/data3/dengkw/projects/open-claude-code-main-merge`, HEAD `65a61706500408ff3935a99f71ca7ec68fbe24c8`.
- No product, test, configuration, board, engineer branch, or credential changes. No real model/provider calls. Only this report and the separately authorized isolated test output directory are writable for this task.
- Parent's log: `/media/data3/dengkw/tmp/oc-capabilities-completion-20261114/harness-baseline.log`: **86 passed, 2 failed, 13 warnings, exit 1**. Both failures are exact tool-name sets with precisely three unexpected entries: `file_read`, `file_write`, `file_search`.
- Root cause: stale integration-test expectations, not evidence of a newly exposed native filesystem tool. Normal-deployment workbench file tools predate this capability-completion work. Their registration is intentional and their execution remains host-mediated.
- This conclusion is limited to the two reported failures. It does not certify all file-tool security properties; explicit real-bridge file-write denial/revocation tests remain important.

## Evidence and history

1. `b69f1f020c97be433cb80567613d1e66023840c2` (2026-11-12), `feat(oc-t3): add controlled file tools, isolated subagents and plugin governance`, explicitly introduced conversation-scoped file read/write/search, path containment, revision checks, and write approval. Its capability test expectations included the three tools.
2. `c1c5a3c152bc78ac352e0686129a207a879f7117` (2026-11-13), `chore: integrate school-cloud product baseline`, introduced the shared workspace registry and real integration tests. Blame places both stale expected sets and registry additions in this integration commit. The discrepancy already exists before the current OCR/image work.
3. `server/app/workspace_tools.py:114` registers all three workbench tools independent of assistant knowledge/web switches; the final list is filtered through the deployment policy at line 151. `server/tests/test_workspace_tools.py:93` explicitly expects generation tools plus file tools when both knowledge and web are disabled.
4. `server/harness/profile.py:18` and `server/harness/workspace_plugin.ts:10` both explicitly allow the three file-tool names. The plugin forwards `workspace/tool` requests to Python with session/call/name/arguments; it does not implement unrestricted file I/O. Native tools/plugin-manager modules are not loaded by this profile.

## Why these fixtures legitimately see the tools

- `server/tests/test_harness_real_runtime.py:220`: Settings select Harness but do not select trial deployment. This normal-mode assistant has knowledge enabled and web disabled. The assertion at line 268 expects knowledge x 2, generation x 2, memory x 3; it omits workbench files x 3. Expected total is 10 for the existing baseline. The early assertion prevents later actions in this particular scenario from being exercised; the baseline log cannot prove that scenario's later assertions passed.
- `server/tests/test_harness_real_mcp.py:150`: normal deployment; business `allowed_tools` contains four explicit external tools, and knowledge/web are disabled. The assistant's connection allowlist constrains business MCP tools, not the separate conversation-owned workbench. Expected names are generation x 2, memory x 3, files x 3, plus four explicit hashed external aliases. Hidden/ungranted business tools must remain absent.
- Both tests use synthetic/local model/MCP fixtures in the supplied baseline run; that is integration evidence, not real-provider capability validation.

## Actual execution boundaries

- `server/app/service.py:648`: deployment policy precedes tool-specific authorization, and enabled-user/usable-assistant checks are refreshed. Around line 672, file search validates the query; file read/write validate the workbench path. Read/search can be automatically authorized; **write returns False**, meaning approval is required, not permission granted.
- `server/app/security.py:80`: file read/search are low-risk reads; file write is medium risk and requires user confirmation.
- `server/app/harness_tools.py`: the host validates session/run/call identity, duplicate calls, tool names and argument schema; applies live authorization and model/human/dual approval as appropriate; rechecks active run/config/policy before dispatch. A failed approval does not dispatch the handler.
- `server/app/file_tools.py`: root is `data/workspaces/<conversation_id>/workbench`; absolute/traversal/hidden paths are rejected. Read/search require current-conversation database registration and verify content hash/size. UTF-8 content, bytes, read length, query length and search count are bounded. A random file merely present in the workbench is not readable/searchable as a registered file.
- File write uses optimistic `expected_revision`, rejects existing untracked files, writes a temporary file, and couples replace/version-history changes with rollback logic. It is not a general-purpose native Write/Bash tool.
- No separate per-assistant file toggle exists in this baseline: normal-mode workbench availability is the intended product boundary, while the deployment allowlist gates trial. Changing that product boundary is not an appropriate fix for stale tests.
- Residual hardening note, not the root cause: the file implementation uses path resolution/stat/read operations rather than an audited same-file-descriptor `O_NOFOLLOW` snapshot. This review does not establish race-free defense against an attacker already able to mutate the server filesystem concurrently. Do not equate static path tests with that stronger claim.

## Minimum safe correction (orange owner)

1. Add literal `file_read`, `file_write`, `file_search` to the two failing expected sets only. Retain exact equality and the independently maintained literal expected set. Do not weaken assertions to subset inclusion, copy the production registry into expected values, delete intentional file registration, or widen the trial allowlist.
2. Retain native-name exclusion (`Read`, `Write`, `Edit`, `Glob`, `Grep`, `Bash`, `Agent`), hidden/ungranted external-alias exclusion, and knowledge/web absence appropriate to each fixture. Explicit negatives make the security intent easier to maintain even where exact sets already imply absence.
3. OCR/image integration must update the profile/plugin fixed name allowlists consistently with Python registration; otherwise new tools will be rejected by real Harness before reaching execution. This is a separate integration task, not a reason to loosen either fixed allowlist.

## Necessary negative coverage

- Real host bridge with `file_write`: before approval no file/version mutation; denied/expired approval causes zero writes; allowed exact call executes once. `Service.authorize_tool(...) is False` must never become a successful approval or a handler authorization bypass.
- Cancellation/revocation while a write waits: no dispatch/file/version mutation after the cancel/revoke; an old call or receipt cannot be reused in a new run/session or with altered path/content/revision. Use the existing host approval system, not a parallel file-specific bypass.
- Conversation isolation: a file registered to conversation A cannot be read/searched from B, even with the same relative name; absolute/traversal/hidden paths and symlink-outside attempts fail; physically present but unregistered files fail; stale revision leaves existing content/history intact.
- Trial: none of the three file tools appear in SDK/Harness registry/profile-facing definitions; forged direct service/bridge invocation fails before approvals/events/evidence/filesystem mutations. Existing `test_trial_authorize_tool_top_level_and_registration_filtering` already covers all three service-level denials, registry absence, and forged `file_write` bridge rejection. Extend bridge negatives to read/search if maintaining exhaustive triplet coverage.
- Native tools remain unavailable even when a historical assistant/run snapshot attempts to reintroduce them. Existing historical-snapshot/legacy-SDK test exercises this boundary without a real SDK process.
- Do not claim existing `test_file_tools.py` proves real approvals: its runtime fixture supplies `check_tool=lambda ...: True`; it tests storage/isolation behavior and a simulated denial, not the actual approval bridge.

## Authorized additional verification

Confirmed existing filenames: `server/tests/test_workspace_tools.py`, `server/tests/test_file_tools.py`, and related trial cases in `server/tests/test_deployment_policy.py`.

Planned command: existing local virtualenv Python, `-B -m pytest -q`, both file/registry modules plus trial authorization/registration and historical-snapshot/legacy-SDK cases. All TMPDIR, pytest basetemp, pytest cache, application data and logs are isolated under `/media/data3/dengkw/tmp/oc-capabilities-completion-20261114/file-tool-baseline`. Provider/auth environment is not inherited; only non-secret basic process environment is retained. Local lexical embeddings, offline model-library flags, and synthetic fixture runtimes prevent model/provider use. The legacy SDK case replaces the SDK client with a local stub.

Execution completed: **22 passed, 1 warning, exit 0** (pytest 15.37 s; wrapper elapsed 16.402 s). The sole warning is Starlette's httpx TestClient deprecation, not a failure. No new or modified tests are part of this audit.

Artifact directory: `/media/data3/dengkw/tmp/oc-capabilities-completion-20261114/file-tool-baseline/run-20261114T103012Z`.

- `command.txt`: exact executable/arguments.
- `command.json`: working directory, argv, explicit non-secret environment overrides and basic inherited environment key names; no provider credentials inherited or logged.
- `pytest.log`: complete test output.
- `result.json`: exit code and elapsed seconds.
- `tmp/`, `pytest/`, `cache/`, `appdata/`, `model-cache/`, `xdg-cache/`: isolated runtime and test artifacts. No global cache clear or source bytecode writes.

Executed command (working directory `open-claude-code-main-merge/server`):

```sh
/media/data3/dengkw/projects/open-claude-code/.venv/bin/python -B -m pytest -q \
  tests/test_workspace_tools.py \
  tests/test_file_tools.py \
  tests/test_deployment_policy.py::test_trial_authorize_tool_top_level_and_registration_filtering \
  tests/test_deployment_policy.py::test_trial_historical_snapshot_and_legacy_sdk_cannot_expose_native_tools \
  --basetemp=/media/data3/dengkw/tmp/oc-capabilities-completion-20261114/file-tool-baseline/run-20261114T103012Z/pytest \
  -o cache_dir=/media/data3/dengkw/tmp/oc-capabilities-completion-20261114/file-tool-baseline/run-20261114T103012Z/cache
```

The additional run confirms existing registry metadata/switch behavior, bounded workbench operations/isolation/revisions, and current trial exclusion paths. It does not rerun or amend the two failing real-Harness tests, execute a real model, establish provider capability correctness, or substitute for the proposed real file-write approval negatives. Post-run `git status --short` and `git diff --stat` are empty; HEAD remains `65a61706500408ff3935a99f71ca7ec68fbe24c8`.
