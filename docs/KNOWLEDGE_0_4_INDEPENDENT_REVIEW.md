# Independent knowledge/evidence review

Reviewed 2026-11-05. Read-only implementation review; findings sent to root and knowledge owner. Reproduction uses only an isolated local fixture database, not deployed/customer data.

| Severity | Finding | Reproduction / required check |
| --- | --- | --- |
| P1 | A deleted knowledge document's source excerpt remains readable. `EvidenceService._authorize` validates its KB but does not revalidate the source document/version. Original/chunk APIs deny the deleted document while the source API still serves its saved content. | Register a source, mark/delete its document, then call `evidence.get`; currently succeeds. Recheck the document/version on source reads and historical finalization. |
| P1 | Revoking an individual MCP tool does not revoke its saved source details if the connection remains assigned. Source authorization checks only the connection ID. | Register a result from `sensitive_lookup`, remove that tool from the assistant allowlist, then read its source; currently succeeds. Persist the actual originating tool name from the trusted runtime hook and validate the current allowlist. Do not trust MCP-supplied `locator.tool` for this decision. |
| P2 | A foreign conversation's internal source link with a query suffix survives finalization as a clickable link. Exact raw-marker/direct-link tests do not cover normalized URLs; client falls back to an ordinary anchor for this form. | Finalize `[9](/api/v1/sources/<foreign-id>?unregistered=1)`: unchanged text and empty registered citations. Normalize internal source URLs and restrict source controls/links to the registered IDs for that answer. |
| P2 | DOCX content is always assigned page 1, even after an explicit page break; the source footer presents that as a real page number. | Upload a DOCX with `<w:br w:type="page"/>` before a second paragraph: second paragraph is indexed at `page=1`. Use paragraph/section provenance and omit unverified physical pagination, or distinguish logical text sections from actual PDF pages. |

Reproduction command: `server/.venv/Scripts/python.exe .runtime/core04_knowledge_review.py`. Result: all four conditions reproduced; private JSON report at `.runtime/core04_knowledge_review.json`.

Other checks: current KB scope/assistant binding and same-conversation historical source checks are present; document versions are pinned and raw originals retained. PDF page/text/pixel bounds, DOCX ZIP expansion/XML entity checks and text/chunk caps are present. No claim of hostile PDF parser isolation or load qualification is made.

Evidence boundary: installed SDK types declare `PostToolUse.tool_response` as `Any`. Current adapter handles dictionary `structuredContent`/`content` shapes. Actual-provider acceptance must verify a genuine MCP result produces registered database sources and inspect its hook shape if none appear. This is a verification gap, not a confirmed shape defect.

## Independent fix recheck

The owner's 18 knowledge/evidence tests passed independently using `--basetemp=.runtime/knowledge-independent-recheck-01 -p no:cacheprovider`. Updated isolated probe `.runtime/core04_knowledge_recheck.py` confirms both P1 findings and the DOCX pagination finding are resolved: deleted documents and removed originating tools deny source details; DOCX uses paragraph provenance with `page=null`. Original query/fragment source aliases are stripped.

The source URL normalization finding remains open for percent-encoded paths: `[9](/api/v1/%73ources/ID)` survives finalization with no registered citations and the client renders it as an ordinary anchor. An independent FastAPI TestClient probe confirms that path decodes to `/api/v1/sources/ID` and matches the source route. Owner/root/client owner notified. The same fix should cover normalized dot segments and Markdown URL decoding and should keep unregistered internal source links inert during streaming too. Proof: `.runtime/core04_knowledge_recheck.json` (`encoded_internal_link_retained=true`).

## Final backend closure and deployed migration check

After HTML entity decoding, bounded URL decoding and normalized path checks were added, the independent original probe reports all four defect conditions false, including the percent-encoded source path; DOCX page is null. The complete knowledge/evidence subset passes **23/23**, including query/fragment, absolute/reference, percent-encoded slash/name, dot-segment and HTML-entity variants. Command: `server/.venv/Scripts/python.exe -m pytest server/tests/test_knowledge_evidence.py -q --basetemp=.runtime/knowledge-independent-recheck-final -p no:cacheprovider`. All four backend findings are closed. The updated private recheck report preserves the final results; the prior failure is described above and in tool evidence.

Read-only Tencent database inspection used SQLite URI `mode=ro`, with no service initialization or mutations: `quick_check=ok`, zero foreign-key violations, expected additive 0.4 fields present, including the separate trusted evidence tool name. Observed 72 conversations and 214 messages; 27 assistants and 18 connections have team scope; 3 team Skills and 2 team knowledge bases exist; no private resource has a missing/unknown owner. These observations verify deployed schema and structural consistency, not a full byte-for-byte history comparison.

Client owner added registered-ID membership checks and reported 10 focused UI tests plus TypeScript/Vite passing. Independent source readback confirms `AnswerMarkdown` enables source controls only when the exact source ID occurs in the current answer's citations; normalized internal URLs without that membership render inert `[来源]` text. Streaming text without citations is therefore inert. The reported UI component tests were not rerun by this reviewer; backend source authorization/normalization and the source-level UI recheck are complete.
