# Knowledge and verifiable sources

Knowledge bases are independent permission-controlled resources. Members own private collections; administrators manage team collections. Assistant bindings select which collections a session may use. Each run pins document versions at admission and rechecks current authorization when using a tool or opening a source.

Original PDF, DOCX, Markdown, TXT, CSV and JSON files are retained with SHA256 and immutable numbered versions. PDFium reads PDF text and renders scanned pages; RapidOCR runs Chinese/English OCR on the server CPU. No company document is uploaded to an external OCR or embedding service. The initial limits are 20 MB per file, 200 PDF pages, 2 million extracted characters and 5,000 chunks; over-limit input is rejected with `DOCUMENT_TOO_LARGE` and leaves no ready index rather than being silently truncated. Failed or empty extraction is never marked ready. A scan without installed OCR is marked `needs_ocr`; retry indexing after fixing the installation. DOCX pages are represented as a paragraph stream, not fabricated physical page numbers.

Every upload, new version and reindex runs through one ingestion job ledger with stages `prepare → parse (→ OCR) → chunk → embed → graph → done`. The job records status, current stage, attempt count, timestamped events, error code/message and cancellation state, and clients can poll `GET /knowledge-documents/{id}/ingestion`. Uploads return immediately with `?background=true`; the background worker also resumes work on process start. Transient environment failures (`PARSER_UNAVAILABLE`, `EMBEDDING_UNAVAILABLE`, `INDEX_UNAVAILABLE`) retry with bounded exponential backoff (3 attempts by default), while bad format, empty text, missing OCR and over-limit input fail once. Cancellation is cooperative between stages and PDF pages; an interrupted lease is requeued while attempt budget remains, and an exhausted job stays terminally `failed` with an explicit retry action. Prior ready versions remain retrievable while a new version is processing or failed.

The delivered semantic configuration combines local `BAAI/bge-small-zh-v1.5` ONNX embeddings (512 dimensions) with BM25 lexical ranking and reciprocal-rank fusion. The default lightweight fallback is explicitly named `local-lexical-v1`; it uses hashed lexical vectors and is **not semantic embedding**. API retrieval metadata reports the actual method. Changing the configured embedding model requires reindexing existing documents. Existing original versions and historical source excerpts remain intact.

On Debian/Ubuntu, install `libgl1` and `libglib2.0-0` for RapidOCR's OpenCV runtime (`sudo apt-get install --no-install-recommends libgl1 libglib2.0-0`). The server Dockerfile includes these OS dependencies; this change does not itself establish container execution acceptance. Install Python dependencies from `server/pyproject.toml`, then run the following with the server environment's Python:

```sh
python scripts/prepare_knowledge_models.py --cache-dir /controlled/path/knowledge-models --download
```

This downloads only public model files, pins repository revision `46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59`, verifies the model SHA256 and performs a local embedding. Configure the environment values printed by the command before starting the server. For offline delivery, copy the five verified files and use `--cache-dir /controlled/path/knowledge-models --model-path /controlled/path/bge-small-zh-v1.5`; routine indexing then makes no model download requests. Keep package/model license notices with any redistributed binary dependencies. The application distribution references dependencies rather than bundling their wheel binaries.

Three evidence kinds share one source registry:

- `data`: actual MCP result excerpts and logical database/table/field/filter/time/aggregation metadata. Missing metadata remains explicitly listed; the Agent does not infer a database name or time range.
- `knowledge`: knowledge base, file/version/hash, PDF page or text section/paragraph, extracted excerpt and immutable original link. Uploaded conversation attachments can also register their real file source with conversation ownership checks.
- `web`: fetched URL/title/excerpt and access time, with publication time when actually available. Search snippets and page content keep distinct content types.

Tools register opaque source IDs. The model receives `[[source:ID]]` and the server converts validated IDs to numbered links and a source list. Invalid IDs and IDs from other conversations cannot produce valid source links. Same-conversation historical sources are reauthorized and copied into the new run. Persisted message citation metadata is minimal; excerpts and locators are obtained from the permission-checked source endpoint. An original answer remains part of conversation history after permission changes, while the revoked source cannot be reopened.

MCP connectors should return a `structuredContent.sources` list (or the same JSON object in a text block):

```json
{
  "sources": [{
    "kind": "data",
    "title": "L1 quality inspection",
    "excerpt": "{\"passed\":97,\"total\":100}",
    "locator": {
      "data_source": "Quality service",
      "database": "quality",
      "schema": "main",
      "table": "inspection_lots",
      "fields": ["passed", "total", "inspected_at"],
      "filters": {"line": "L1"},
      "time_range": {"from": "2026-11-01", "to": "2026-11-02"},
      "timezone": "Asia/Shanghai",
      "queried_at": "2026-11-05T10:00:00+08:00",
      "aggregation": {"numerator": 97, "denominator": 100, "operation": "sum(passed)/sum(total)"}
    }
  }]
}
```

Use actual tool results for these fields. Credentials, connection URLs containing secrets and server filesystem paths are not evidence locators.

Primary implementation references: [PDFium Python API](https://pypdfium2.readthedocs.io/en/stable/python_api.html), [PDFium package licensing](https://github.com/pypdfium2-team/pypdfium2/blob/main/README.md#licensing), [FastEmbed supported models](https://qdrant.github.io/fastembed/examples/Supported_Models/), [BGE ONNX model files](https://huggingface.co/Qdrant/bge-small-zh-v1.5/tree/46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59).
