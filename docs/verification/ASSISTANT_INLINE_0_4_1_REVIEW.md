# Assistant inline configuration review (0.4.1)

Date: 2026-11-05. Reviewer: core agent. Status: API review, authorized concurrency fix, independent client source review and regression review complete. No finding remains open within this change. Root owns final installed-client browser acceptance.

## API and permissions

- `GET /assistants/{id}` allows the owner of a private assistant and members authorized to use an enabled team assistant. An admin may manage a disabled team assistant but cannot read another member's private assistant. `can_edit` is the returned authority for exposing save controls; team members can inspect/use shared assistants without editing them.
- Connection, knowledge-base and Skill lists contain all currently readable, nondeleted resources, including disabled resources. The form must retain disabled bindings. Team assistants can bind only team resources; private assistants can bind their owner's resources and readable team resources.
- PATCH omits unchanged top-level fields without clearing them. Explicit empty lists clear capability bindings. Explicit null clears `default_model_id` / `default_reasoning_level`; other nullable fields currently treat null as no change. `connections` and `access` are complete replacement values, so their nested fields must be supplied together rather than treated as nested patches.
- Missing/deleted bindings and changed MCP tool catalogs are revalidated when saving. Do not silently discard IDs absent from a loaded list: show unavailable bindings and let the user explicitly remove them. Otherwise a basic-information edit can fail with an opaque missing-resource or invalid-tool-selection response, while filtering them automatically would silently change capabilities.

## Found and fixed: competing assistant writes

The prior implementation read a row, merged the request, then updated every assistant field without a version condition. Two concurrent readers could silently overwrite one another, even with different-field PATCH requests.

Root authorized a narrowly scoped repair, without a database migration:

- `AssistantPatch.expected_revision` is optional and must be at least 1 when supplied. The client should submit the revision obtained by its detail GET.
- Authorization runs before the revision comparison. A stale supplied revision returns HTTP 409 with `error.code=ASSISTANT_CONFLICT` and the message “助手配置已更新，请重新加载后再保存”.
- The UPDATE also requires the revision read for this operation and `deleted=0`. A competing save or delete that wins after the initial read produces the same 409, including for legacy clients that omit `expected_revision`.
- Independent review also found that a global reread after the write could return another owner's subsequent private revision. The response now contains only the exact values and revision successfully written by this authorized request, with no global post-write reread. A dedicated regression changes the next version to another owner's private configuration between the UPDATE and response assembly and confirms it is not disclosed.
- Existing nonconcurrent callers remain compatible. The client must keep the draft on a conflict and offer a deliberate reload/review; it must not silently resubmit against a newer revision.

Changed code: `server/app/models.py`, `server/app/service.py`. Added four regression tests in `server/tests/test_resources_04.py`, covering stale revision/no mutation, omitted-field preservation, explicit capability/default clearing, competing legacy writes, deletion between read/update and a later private revision excluded from the response. Final focused resource/capability/API suite: **25 passed** using `.runtime/assistant-config-core-041-race` as test base. Tests use isolated local fixtures and the injected fake runtime; no paid model or external MCP calls were made.

The independent knowledge reviewer reran its original nested-save response-race probe after the fix: the response retained shared revision 2, omitted the owner's private revision 3, and the subsequent unauthorized GET was denied. Its 12 resource tests and four independent probes passed (16/16), including two Service instances on separate SQLite connections and authorization preceding conflict checks. No backend finding remains open for this change.

## Independent client source review

Reviewed `AssistantConfiguration.tsx`, `useAssistantConfiguration.ts`, `AssistantDetailPage.tsx`, `AssistantsPage.tsx`, `App.tsx` and `NewSession.tsx` after the shared form and navigation integration landed.

- Detail configuration loads its authorized resource and capability lists afresh. Its AbortController and aborted-signal checks prevent an old load from replacing a later assistant or reload. An assistant-ID key separates detail instances.
- The read-only state uses `can_edit`, disables the shared fieldset and omits save buttons. Editable saves send changed top-level fields with a complete changed connection/access value. `baselineRevision` belongs to the loaded draft baseline and changes only with a successful save or explicit form recreation; a parent refresh cannot pair an old draft with a newer revision.
- Selected disabled resources remain visible. Missing connections, knowledge bases, Skills and missing discovered tools remain in the draft with explicit removal controls. Selecting/disabling discovered tools does not silently erase missing tool grants.
- The shared App guard covers sidebar navigation, recent-session opening, assistant opening, new-session action, logout, detail tabs and editor close. Its synchronous saving ref prevents navigation during save. Browser unload is guarded. Confirmed discard increments a form epoch, so an inline form left behind a new-session modal does not retain discarded inputs.
- A failed save leaves the draft/baseline intact. A 409 displays an explicit reload action through the dirty guard. Successful save updates both the draft baseline/revision and the parent snapshot. No automatic conflict retry is present.
- NewSession fetches the current assistant before enabling creation, cancels older requests after assistant switches and applies defaults only once per fetched snapshot. Waiting for the model list does not use stale home-card defaults, and later catalog/parent renders do not reset an intentional model choice.

Independently read `client/src/assistantConfiguration.test.tsx`: its seven focused cases check hidden-binding preservation and deep copies, explicit clears/full nested payloads, baseline revision and a single 409/no retry, complete read-only rendering, selected disabled resources, shared cancellation signals/member-safe resource loads, and model/default-level selection. The client owner reports **45/45** total tests and a successful TypeScript/Vite build. Those tests substantiate the listed helper/rendering contracts; they do not simulate full browser navigation or deliberately delayed React effects.

Root reports Linux full regression **225 passed** before the final response-race adjustment, followed by the final relevant **25 passed** after it, the independent **16/16** checks and an actual-service stale-revision 409. The focused post-fix checks avoid claiming the earlier full suite ran against a later code state.

### Closure checklist

- [x] Assistant detail/resource ACL contract and edit controls reviewed.
- [x] Omitted-field/default-clear/nested-replacement behavior reviewed.
- [x] Concurrent write and post-write private-response races fixed and independently rechecked.
- [x] Hidden/disabled binding preservation and explicit removal reviewed.
- [x] Dirty/saving navigation, stale response guards and fresh session-default logic source-reviewed.
- [x] Focused client regression coverage and reported complete test/build results reviewed.
- [x] Findings, files, commands and evidence boundaries recorded; no additional source change requested.

Dynamic multi-window, slow-response and navigation behavior remains part of root's installed-client browser acceptance, which was underway at closure. This review agent has not changed remote UI state or claimed live browser validation.
