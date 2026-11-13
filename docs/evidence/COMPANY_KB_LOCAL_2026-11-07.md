# Better AI company knowledge base — local evidence — 2026-11-07

## Scope

This is a local school-cloud development import only. The source materials are
company-internal and are not committed, uploaded, or deployed to production.

## Imported sources

The team knowledge base `Better AI 公司知识库（内部）` contains seven reviewed
Markdown sources from `docs/company/`: the company brief, five positioning and
naming notes, and the text-extraction supplement for the business-plan deck.
The runtime stores the source bytes, version, hash, and page/chunk provenance.
The repository README and damaged presentation archive were deliberately not
imported as business knowledge.

## Verification

- All seven documents are `ready`.
- The existing `业务演示助手` is bound to this company knowledge base in
  addition to the two synthetic demonstration knowledge bases.
- A local search for company positioning and delivery stages returned cited
  chunks from `COMPANY_BRIEF.md` and the positioning notes.
- The knowledge graph returned 120 bounded nodes and 78 edges for the current
  query limit.
- Retrieval is currently `BM25 + local-lexical-v1`; semantic BGE vectors are
  not claimed because no offline BGE model is configured.

## Deferred source work

The 37-page PDF is retained as an internal source reference, but its first 33
pages are image-only and the local OCR dependency is absent. It is not claimed
as a complete indexed PDF. The PPTX file is truncated/invalid and remains
excluded until a repaired source is provided.
