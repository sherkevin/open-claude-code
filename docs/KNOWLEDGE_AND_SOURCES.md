# Knowledge and verifiable sources

Knowledge bases are independent permission-controlled resources. Members own private collections; administrators manage team collections. Assistant bindings select which collections a session may use. Each run pins document versions at admission and rechecks current authorization when using a tool or opening a source.

Original PDF, DOCX, Markdown, TXT, CSV and JSON files are retained with SHA256 and immutable numbered versions. PDFium reads PDF text and renders scanned pages; RapidOCR runs Chinese/English OCR on the server CPU. No company document is uploaded to an external OCR or embedding service by default. The initial limits are 20 MB per file, 400 PDF pages and 2 million extracted characters. Textless images can be ready from a visual index; ordinary empty text documents are never marked ready. A scan without installed OCR is marked `needs_ocr`; retry indexing after fixing the installation. DOCX pages are represented as a paragraph stream, not fabricated physical page numbers.

The delivered retrieval configuration combines local `BAAI/bge-small-zh-v1.5` ONNX embeddings (512 dimensions) with BM25 lexical ranking and reciprocal-rank fusion. The default lightweight fallback is explicitly named `local-lexical-v1`; it uses hashed lexical vectors and is **not semantic embedding**. The human-facing knowledge page exposes three bounded choices: `混合 RAG` (reciprocal-rank fusion), `BM25 关键词` (exact lexical ranking), and `本地向量` (local vector ranking). BM25 is deliberately independent of optional embedding model availability, so a user can still perform exact keyword retrieval offline. Every search response reports the selected method, per-hit lexical/vector scores, rank, immutable document version, and a copyable context preview capped by the requested character limit. The preview is assembled only from permission-checked current versions and is not sent to an external model. Changing the configured embedding model requires reindexing existing documents. Existing original versions and historical source excerpts remain intact.

Image versions additionally receive a local dimensions/format record and a bounded JPEG thumbnail (`GET /api/v1/knowledge-documents/{id}/thumbnail`). They also receive at most 16 deterministic grid-region previews (up to 4×4), each carrying source pixel coordinates and a version-bound thumbnail (`GET /api/v1/knowledge-documents/{id}/regions` and its region thumbnail endpoint). PDF versions may expose the first twelve page previews through the same endpoint with `page=`. The shared development runtime can load a local OpenAI CLIP ViT-B/32 checkpoint for 512-dimensional image and text vectors, and RapidOCR for text boxes; vector results remain version-bound and do not imply semantic segmentation. Mixed/vector retrieval can return a textless image as an explicit `visual_embedding` hit. When OCR or the local model is unavailable, the corresponding state remains `needs_ocr` or `needs_model`; no pixel-derived entity or relationship is presented as fact.

On Debian/Ubuntu, install `libgl1` and `libglib2.0-0` for RapidOCR's OpenCV runtime (`sudo apt-get install --no-install-recommends libgl1 libglib2.0-0`). The server Dockerfile includes these OS dependencies; this change does not itself establish container execution acceptance. Install Python dependencies from `server/pyproject.toml`, then run the following with the server environment's Python:

```sh
python scripts/prepare_knowledge_models.py --cache-dir /controlled/path/knowledge-models --download
```

This downloads only public model files, pins repository revision `46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59`, verifies the model SHA256 and performs a local embedding. Configure the environment values printed by the command before starting the server. For offline delivery, copy the five verified files and use `--cache-dir /controlled/path/knowledge-models --model-path /controlled/path/bge-small-zh-v1.5`; routine indexing then makes no model download requests. Keep package/model license notices with any redistributed binary dependencies. The application distribution references dependencies rather than bundling their wheel binaries.

The school-cloud development environment also has an opt-in OpenAI Chat
Completions visual adapter at `server/app/openai_visual.py`. Its verified
public catalog target is `deepseek-v4-flash-202605`; production traffic must
use the Tencent TokenHub route. A loopback gateway configured through
`AGENT_KNOWLEDGE_VISION_BASE_URL` is allowed only as a controlled development
bridge whose upstream remains TokenHub, and is disabled unless
`AGENT_KNOWLEDGE_VISION_ENABLED=true`. The adapter accepts only bounded image
bytes, records latency and a response digest, and never stores raw responses or
tokens. Visual, entity and relation roles are reported by the authenticated
`/api/v1/models/provider-preflight` endpoint and remain unavailable or disabled
when their catalog capability is not explicitly configured. A successful
protocol probe is not a semantic graph-quality acceptance.

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
