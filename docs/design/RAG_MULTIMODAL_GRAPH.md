# Multimodal RAG and knowledge graph design

Status: design proposal with local visual-index and controlled model-candidate
evidence, 2026-11-07. The repository ships permission-checked local image
metadata, JPEG thumbnails, a bounded deterministic grid of version-bound
region previews, local CLIP image/text vectors, and an explicitly gated
OpenAI-compatible visual candidate stage. It does not claim semantic graph
quality acceptance.

## Design boundary and existing contracts

The current implementation is the authority for behavior that already exists:

- A knowledge base is a permission-controlled `private` or `team` resource.
  Assistants bind explicit `knowledge_base_ids`; a run pins each document's
  current immutable version at admission. Retrieval and source reopening
  re-check authorization. See [API contract](../API_CONTRACT.md),
  [knowledge implementation](../KNOWLEDGE_AND_SOURCES.md), and
  [server/app/knowledge.py](../../server/app/knowledge.py).
- Uploads retain original PDF, DOCX, Markdown, TXT, CSV, and JSON bytes with a
  SHA-256 hash and numbered versions. PDF text is read with PDFium and scanned
  pages use local RapidOCR. Text is chunked with page/section/paragraph and
  extraction offsets. The current limit is 400 PDF pages, 2 million extracted
  characters, and 5,000 chunks per document.
- Retrieval is server-local hybrid BM25 plus the configured local embedding
  model. `local-lexical-v1` is explicitly a lexical fallback, not a semantic
  model. The result carries the actual retrieval mode and model name.
- Evidence is registered from actual tool results. Knowledge evidence contains
  file/version/hash and a PDF page or extracted paragraph. The model sees an
  opaque `[[source:ID]]`; finalization validates that ID, creates links, and
  re-authorizes the source. It never accepts an invented filesystem URL.
- Resource access is currently defined by `owner_id`, `scope`, `enabled`, and
  assistant binding. Team resources are administrator-managed; administrators
  do not automatically read another member's private content.

Semantic graph quality remains proposed. The bounded image-asset phase stores
dimensions, format metadata, a local thumbnail, and at most sixteen
deterministic grid regions (each with source coordinates and a small preview).
Local CLIP can index images and return a version-bound visual result for a
textless image; missing dependencies remain truthful `needs_ocr`/`needs_model`
states. The grid is a layout preview rather than object detection or semantic
segmentation. No claim below implies that an external OCR, embedding, or model
service may receive customer files.

## Goals and non-goals

The extension should answer questions that need text, page layout, figures,
tables, OCR, or relationships among documents while preserving a citation to
the exact immutable source. It should support local/offline indexing, bounded
cost, tenant isolation, and incremental updates.

It is not a replacement for the business system behind an MCP connection, a
general-purpose public knowledge graph, or an automatic authority system. A
graph assertion is an extracted hypothesis until it is backed by one or more
source spans and the answer shows that evidence.

## Proposed architecture

```text
original bytes + metadata
        |
        v
version admission (sha256, MIME/size limits, ACL, parser profile)
        |
        +--> layout/text/OCR/table/figure extraction --> canonical parts
        |                                                 |
        |                                                 +--> lexical index
        |                                                 +--> text vectors
        |                                                 +--> page/region visual vectors (optional)
        |                                                 +--> entity/claim graph deltas
        v
immutable content manifest + provenance links + index/graph epochs
        |
query --> permission-filtered lexical/dense/visual candidates
        |       + graph entity/path expansion (visible nodes only)
        v
rerank and context pack --> model --> registered evidence finalization
```

Every derived object carries `organization_id` (the deployment's organization),
`knowledge_base_id`, `document_id`, `version`, `parser_profile`, and an
`acl_epoch`. A derived object is usable only when its source version is still
retained, its index epoch is ready, and the caller can access the source under
the current policy.

### Ingestion and canonical content

1. **Admit and fingerprint.** The upload endpoint keeps the existing immutable
   version flow. Detect MIME from bytes plus extension, reject mismatches and
   unsupported files, bound decompressed/archive size, and write the original
   under the existing private knowledge root. `sha256` is the content identity;
   the document/version ID is the user-visible identity.
2. **Parse without flattening structure.** A parser adapter emits ordered
   `page`, `paragraph`, `heading`, `list`, `table`, `table_cell`, `figure`,
   `caption`, `formula`, and `image` parts. Each part has normalized text (when
   present), `page`, `bbox` in source-page coordinates (when available), an
   ordinal/reading-order key, and an extraction method. Existing PDFium/RapidOCR
   output remains a valid adapter. A layout-aware adapter may use Docling's
   layout and table pipeline, which documents headers, sections, tables, images,
   captions, reading order, and OCR [in its conversion guide](https://github.com/docling-project/docling-graph/blob/main/docs/fundamentals/extraction-process/document-conversion.md)
   and [CLI reference](https://docling-project.github.io/docling/reference/cli/).
3. **OCR and confidence.** For an image-only page, retain the rendered page
   artifact and OCR tokens/lines with confidence and bounding boxes. Mark the
   extraction method (`pdf-text`, `local-ocr`, or another configured local
   adapter). If OCR is unavailable, keep the existing `needs_ocr` status and do
   not expose the document to retrieval. Low-confidence OCR remains searchable
   only when the query path explicitly allows it and is labeled in evidence.
4. **Tables and figures.** Store a table as a parent part plus ordered rows,
   columns, and cells. Keep both a lossless cell representation (including
   spans/merged cells when known) and a readable rendering such as Markdown for
   embedding. Store figures/images as content-addressed artifacts with a
   `content_url` that uses the same authorization checks as original files.
   Captions and nearby text are separate parts linked to the figure; never
   represent a chart's pixels as if they were verified text.
5. **Chunk by structure.** Text chunks may retain the existing paragraph offsets.
   New chunks include `part_ids`, heading breadcrumb, table/figure IDs, page and
   bounding-box ranges, and a stable `chunk_key` derived from the version hash,
   parser profile, and normalized part sequence. A table chunk includes its
   header plus the rows needed to interpret each selected cell; a figure chunk
   includes caption/OCR plus a pointer to the image. Chunks never cross an
   authorization boundary.
6. **Index and manifest.** Write a manifest only after all required derived
   artifacts succeed. It records parser/model versions, extraction counts,
   OCR/table/figure status, and index epochs. A failed or partial manifest is
   not `ready`; reindexing the same version is idempotent.

### Modality-specific retrieval

- **Text and OCR:** run the existing BM25/vector retrieval over text chunks,
  with OCR confidence and extraction method available to the reranker. Keep
  lexical retrieval because identifiers, part numbers, and exact table values
  are often more reliable than semantic similarity.
- **Tables:** normalize query mentions of a column, row label, unit, and value;
  retrieve table-level candidates first and then cell/row spans. Do not answer
  an arithmetic question from a lossy prose summary when the relevant cells
  are available. Register the table/cell locator in evidence.
- **Figures and page images:** optionally add local multi-vector page/region
  embeddings. ColPali is a useful research reference because it embeds page
  images and uses late interaction, avoiding a text-only extraction bottleneck
  for layout, figures, and tables ([paper](https://arxiv.org/abs/2407.01449),
  [open implementation](https://github.com/illuin-tech/colpali)). This is an
  optional index tier; the text path remains the fallback when the visual model
  is unavailable. The retrieval result must include the page/region and the
  exact image artifact used.
- **Attachments:** conversation attachments retain their existing owner and
  conversation checks. If an attachment is promoted into a knowledge base, copy
  it into an immutable knowledge version and create a new provenance edge; do
  not silently make a private attachment globally searchable.

## Knowledge graph model

The graph is a versioned, provenance-first overlay, not a second source of
truth. Recommended logical records are:

| Record | Required fields | Purpose |
| --- | --- | --- |
| `graph_node` | `node_id`, `type`, `canonical_key`, `label`, `knowledge_base_id`, `document_id`, `version`, `acl_epoch`, `confidence` | Entity, topic, claim, table, figure, page, chunk, or source anchor. |
| `graph_edge` | `edge_id`, `src`, `predicate`, `dst`, `version`, `confidence`, `assertion_part_id`, `valid_from`, `valid_to`, `acl_epoch` | Typed relationship such as `mentions`, `contains`, `supports`, `depends_on`, `same_as`, or `contradicts`. |
| `graph_assertion` | `assertion_id`, `edge_id`, `extractor_id`, `extractor_version`, `raw_text`, `status` | Keeps the extracted statement and whether it is accepted, disputed, or retracted. |
| `graph_provenance` | `assertion_id`, `document_id`, `version`, `part_id`, `page`, `bbox`, `sha256`, `source_id` | Maps each assertion to an evidence source and exact span. |
| `graph_index_epoch` | `epoch_id`, `kb_id`, parser/embedding/extractor versions, status, created time | Allows atomic publication and rollback of graph/index changes. |

Minimum node types are `Document`, `Version`, `Page`, `Chunk`, `Table`,
`Cell`, `Figure`, `Entity`, `Claim`, and `Topic`. The graph extractor may
propose `Person`, `Organization`, `Product`, `Site`, `Part`, `Metric`, `Date`,
and `Event` as typed entities, but each proposal must point to one or more
source parts. Entity canonicalization is scoped to a knowledge base unless an
administrator explicitly creates a cross-base mapping.

Use a pipeline similar to the public GraphRAG indexing design—entity,
relationship, and claim extraction, community detection, summaries, and
embeddings—while keeping this project's source-version and permission rules.
The GraphRAG documentation describes those stages and warns that indexing can
be expensive ([overview](https://github.com/microsoft/graphrag/blob/main/docs/index/overview.md),
[repository](https://github.com/microsoft/graphrag)). Community summaries are
derived artifacts: they may accelerate global questions but cannot replace the
underlying passages or citations.

## Retrieval and graph-to-RAG flow

1. **Freeze scope.** At run admission, use the existing assistant binding and
   `knowledge_run_documents` snapshot. The graph query receives the same list of
   permitted `(document_id, version)` pairs and the current user/assistant ACL
   context.
2. **Classify the question.** Generate lexical terms, a semantic query, and
   optional entity/relationship hints. The classifier may be heuristic; its
   output is not evidence.
3. **Generate candidates.** Run BM25 and text-vector search over visible text,
   table, OCR, and caption chunks. If enabled, search visual page/region vectors
   and merge by source version. Filter by ACL and snapshot *before* scoring or
   graph expansion; a forbidden result must not affect ranking statistics.
4. **Expand the graph.** Resolve entities from the visible candidates, then
   traverse at most two hops for a local/entity question. For a broad question,
   retrieve visible community summaries and then fetch their supporting chunks.
   Every path node and edge must be visible, version-pinned, and backed by a
   source part. Drop paths containing hidden nodes rather than returning a
   redacted relationship that could disclose their existence.
5. **Rerank and assemble.** Prefer exact identifiers and high-confidence
   source-backed claims; deduplicate overlapping chunks and cap per-document
   context. Context items are typed (`text`, `ocr`, `table`, `cell`, `figure`,
   `graph_claim`) and carry `source_id`, locator, extraction method, confidence,
   and index epoch. The model receives source markers, never raw internal URLs.
6. **Answer and finalize.** The model can cite only registered source IDs. The
   existing evidence finalizer validates markers and creates numbered links. A
   graph claim therefore yields both a graph explanation and the originating
   page/table/figure evidence; unsupported graph summaries are omitted or
   labeled as unverified.

For complex questions, an optional agentic controller can create a small set of
retrieval tasks (for example, identify a part, find its table row, then verify a
figure). NVIDIA's public Agentic RAG blueprint uses scope discovery, targeted
task execution, query retries, synthesis, and an optional verification gate
([design and limitations](https://docs.nvidia.com/rag/latest/agentic-rag.html)).
The same pattern is useful here for multi-document, ambiguous, and numeric
table/chart questions, but it must remain opt-in because each retry adds model
latency and every sub-query must inherit the run's pinned versions and ACL. A
verification pass can request more evidence; it cannot authorize a hidden
document or manufacture a citation.

M$^3$KG-RAG reports two useful multimodal lessons: shared-space similarity can
miss modality-specific evidence, and multi-hop retrieval benefits from grounded
entity selection plus selective context pruning ([CVPR open-access paper](https://openaccess.thecvf.com/content/CVPR2026/papers/Park_M3KG-RAG_Multi-hop_Multimodal_Knowledge_Graph-enhanced_Retrieval-Augmented_Generation_CVPR_2026_paper.pdf),
[NVIDIA Research page](https://research.nvidia.com/labs/lpr/publication/park2026m3kgrag/)).
This design adapts those ideas to document text, OCR, tables, and figures; audio
and video are outside the current upload contract. Modality-wise retrieval and
source-backed pruning are preferred over forcing all artifacts into one vector
space.

For a deterministic rank merge, define `q` as the normalized query, `d` as a
candidate part, `L(q,d)` as its BM25 score, `V(q,d)` as cosine/dense score, and
`G(q,d)` as a bounded graph-support score from visible source-backed neighbors.
After each score is normalized within the same permitted candidate set, use:

$$S(q,d)=w_L L(q,d)+w_V V(q,d)+w_G G(q,d)-w_C C(d).$$

Here `w_L`, `w_V`, and `w_G` are deployment configuration weights,
`C(d)` is a penalty for low OCR confidence, stale index epoch, or duplicate
context, and `w_C` is its weight. The output retains the component scores and
model/index IDs for evaluation; it must not present `S` as a confidence or
truth probability. A visual late-interaction score is an additional candidate
score and is only enabled when its local model/index is ready.

## Provenance and evidence

Use the current evidence registry as the public citation surface. Internally,
the records map naturally to W3C PROV: the original version is a `prov:Entity`,
the parser/OCR/embed/graph extraction is a `prov:Activity`, and each chunk,
vector, graph assertion, and summary is a derived `prov:Entity` linked with
`prov:wasDerivedFrom` and `prov:wasGeneratedBy` ([PROV-O recommendation](https://www.w3.org/TR/prov-o/)).

Required invariants:

- A source locator names the immutable document/version/hash and a page,
  paragraph, cell, figure, or bounding box. It never contains credentials or a
  private filesystem path.
- Every graph assertion stores one or more source parts and extraction metadata.
  A contradiction is represented as another assertion with its own evidence;
  do not overwrite the earlier document version.
- Index/parser/model changes create a new epoch. Rebuilding a vector does not
  alter historical citations; a source opened later still resolves the version
  named by the citation.
- Evidence registration is transactional with the run/tool result. If a
  candidate is discarded or a permission check fails, it cannot mint a source
  ID. Raw model-authored source links remain untrusted.
- A citation to an image or table exposes a safe preview/content endpoint only
  after the same current permission check used for the source record.

## Permissions and isolation

The permission boundary is applied at ingestion, indexing, graph traversal, and
source opening:

1. Derive `owner_id` and `scope` from the authenticated user and existing
   `resource_access`/`resource_editable` rules. Clients cannot assign ownership.
2. Stamp every part, vector, node, edge, artifact, and graph summary with the
   knowledge base and ACL epoch. A team graph and a private graph use separate
   partitions by default.
3. Bind an assistant only to accessible knowledge bases. At run admission pin
   document versions and the graph/index epoch. Current revocation is checked
   again when searching, reading, traversing, and opening a citation.
4. Traverse only visible nodes and edges. Do not build a global shared entity
   index that lets a private entity change public ranking or reveals that a
   private document exists. Cross-base `same_as` edges require explicit admin
   approval and carry both source ACLs.
5. On disable, deletion, or scope change, stop new retrieval immediately,
   tombstone derived artifacts, and deny source reopening. Keep historical
   message text and citation IDs as the current contract does, but do not
   resurrect the revoked bytes or graph paths.

## Incremental updates and recovery

- **No-op upload:** same `sha256`, MIME, and parser profile may reuse the
  immutable version's manifest. Record a new ingestion attempt but do not make
  duplicate chunks or graph nodes.
- **New version:** create the version first, then derive parts/chunks/vectors and
  graph deltas in a staging epoch. Reuse unchanged part keys where the content
  hash and structural path match; invalidate changed parts and their incident
  edges. Publish the epoch atomically only when required artifacts are ready.
- **Deletion/revocation:** mark the document/base deleted or disabled, remove it
  from candidate filters, and write tombstones for index/graph stores. Physical
  retention follows the existing immutable-history policy; tombstones prevent
  retrieval before cleanup completes.
- **Crash/retry:** each job is idempotent by `(document_id, version,
  parser_profile, extractor_version, index_epoch)`. Startup reconciliation marks
  abandoned processing as failed, matching the current knowledge service. A
  retry can resume from the last completed artifact without publishing a partial
  graph.
- **Model/parser change:** keep old epochs for historical runs, build a new
  epoch, compare evaluation metrics, then switch the knowledge base pointer.
  Existing citations continue to resolve their saved source version and
  locator; new runs use the selected current epoch.

## Proposed internal contract (future implementation)

These are logical fields for a follow-up migration, not a claim about current
SQLite tables:

```text
content_parts(part_id, document_id, version, kind, parent_part_id,
  ordinal, page, bbox, text, normalized_hash, extraction_method,
  confidence, artifact_path, source_locator_json)
embeddings(part_id, modality, model_id, dimensions, index_epoch, vector_ref)
graph_nodes(node_id, kb_id, type, canonical_key, label, version,
  confidence, acl_epoch, status)
graph_edges(edge_id, src_node, predicate, dst_node, version,
  confidence, assertion_part_id, valid_from, valid_to, acl_epoch, status)
graph_epochs(epoch_id, kb_id, parser_profile, extractor_version,
  embedding_models_json, status, created_at, published_at)
```

The existing API can remain source-compatible. A future search response may add
`modality`, `part_id`, `bbox`, `table_id`/`cell_id`, `graph_support`,
`index_epoch`, and `source_id` to each result; existing fields such as
`document_id`, `version`, `page`, `section`, `paragraph`, `start`, `end`,
`text`, and `score` remain stable. New administrative endpoints should expose
index/graph status and retry diagnostics, never raw vectors, hidden graph nodes,
or credentials.

## Phased implementation and acceptance

### Phase 0 — contract and measurement

- Freeze schemas and permission invariants above; add fixtures containing clean
  PDF text, scanned Chinese/English PDF, multi-column pages, a table with merged
  cells, a figure/caption, and a private/team cross-scope case.
- Acceptance: current text-only tests remain green; each fixture has a known
  source hash, expected page/part boundaries, and an explicit unsupported or
  low-confidence outcome where appropriate.

### Phase 1 — layout-aware canonical parts

- Add a parser adapter behind the existing indexing service. Emit parts and
  locators while retaining current `knowledge_pages`/`knowledge_chunks` output.
- Acceptance: reindexing is idempotent; table cells, figure regions, OCR method,
  and coordinates are visible to an authorized inspector; originals and old
  citations still resolve; no document bytes leave the server.

### Phase 2 — multimodal indexes

- Add local text, table, OCR, and optional page/region visual indexes. Implement
  permission-first candidate filtering and score-component diagnostics.
- Acceptance: an exact-value/table query, an OCR query, a caption query, and a
  visual-layout query each retrieve the expected immutable version with a valid
  source marker. Missing local visual models produce a truthful unavailable
  status and use the text path.

### Phase 3 — provenance-first graph

- Add graph nodes/edges/assertions, bounded entity extraction, source-backed
  graph expansion, and graph index epochs. Community summaries are optional and
  never replace source passages.
- Acceptance: entity/path questions retrieve only visible nodes, every returned
  edge has source evidence, contradictory versions remain separate, and private
  data cannot change team retrieval scores or be inferred through graph paths.

### Phase 4 — incremental operations and evaluation

- Add staged epoch publication, retries/tombstones, parser/model migration,
  and an offline evaluation set. Track recall@k for text/table/OCR/figure,
  citation validity, ACL leakage tests, indexing latency, and storage cost.
- Acceptance: same-version retries produce no duplicates; revocation removes
  candidates before physical cleanup; old runs retain their pinned evidence;
  release reports include metric deltas and an explicit evidence boundary.

## Risks and evidence boundary

OCR errors, reading-order mistakes, table-cell association, entity
canonicalization, and graph summaries can introduce false relationships. Keep
raw artifacts and confidence visible, prefer passage/table citations over a
summary, and require human review for high-impact graph assertions. Visual
retrieval is model- and hardware-dependent; its latency, memory footprint, and
accuracy must be measured locally before enabling it by default. The references
below establish useful techniques and interfaces, not guarantees for this
repository's data or models.

## References

- [Microsoft GraphRAG indexing overview](https://github.com/microsoft/graphrag/blob/main/docs/index/overview.md) and [repository](https://github.com/microsoft/graphrag).
- [Docling graph document conversion: layout, OCR, and tables](https://github.com/docling-project/docling-graph/blob/main/docs/fundamentals/extraction-process/document-conversion.md); [Docling CLI options](https://docling-project.github.io/docling/reference/cli/).
- [ColPali: Efficient Document Retrieval with Vision Language Models](https://arxiv.org/abs/2407.01449) and [implementation](https://github.com/illuin-tech/colpali).
- [W3C PROV-O recommendation](https://www.w3.org/TR/prov-o/).
- [NVIDIA Agentic RAG blueprint](https://docs.nvidia.com/rag/latest/agentic-rag.html) (optional planning/verification pattern; no runtime dependency).
- [M$^3$KG-RAG CVPR 2026 open-access paper](https://openaccess.thecvf.com/content/CVPR2026/papers/Park_M3KG-RAG_Multi-hop_Multimodal_Knowledge_Graph-enhanced_Retrieval-Augmented_Generation_CVPR_2026_paper.pdf) (modality-wise multi-hop retrieval reference).
- [DeepSeek Harness official preview](https://www.deepseek.com/harness/en/) (consumer harness; retrieval storage, ACL, and evidence remain server-owned and do not depend on this evolving API).
