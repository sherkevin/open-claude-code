"""Synthetic models for exercising the unchanged official HippoRAG pipeline.

These fixtures are not quality or cost benchmarks and never call a provider.
"""
from __future__ import annotations

from collections import Counter
import json
import re
from threading import Lock

import numpy as np


DOCUMENT_FACTS = {
    "Pump P101 supplies valve V20.": [("P101", "supplies", "V20")],
    "Valve V20 requires seal S7.": [("V20", "requires", "S7")],
    "Seal S7 has a service interval of 90 days.": [("S7", "service interval", "90 days")],
    "Compressor C3 requires oil O9.": [("C3", "requires", "O9")],
    "Pump P101 is also called MainPump.": [("P101", "also called", "MainPump")],
}
QUERY = "Which seal is required by the valve supplied by P101?"


class SyntheticLLM:
    """Return fixture outputs through the real OpenIE and fact-filter parsers."""

    def __init__(self):
        self.calls = Counter()
        self.unexpected_calls = 0
        self._lock = Lock()

    def infer(self, messages, **kwargs):
        system = messages[0]["content"]
        prompt = messages[-1]["content"]
        if "[[ ## fact_before_filter ## ]]" in prompt:
            field = prompt.split("[[ ## fact_before_filter ## ]]", 1)[1].strip()
            candidates, _ = json.JSONDecoder().raw_decode(field)
            facts = [fact for fact in candidates["fact"]
                     if not {"c3", "o9"}.intersection(str(x).casefold() for x in fact)]
            response = "[[ ## fact_after_filter ## ]]\n" + json.dumps({"fact": facts})
            response += "\n\n[[ ## completed ## ]]"
            kind = "fact_filter"
        elif "extract named entities" in system:
            facts = DOCUMENT_FACTS.get(prompt)
            if facts is None:
                return self._unexpected()
            response = json.dumps({"named_entities": sorted({f[i] for f in facts for i in (0, 2)})})
            kind = "ner"
        elif "construct an RDF" in system:
            passages = [text for text in DOCUMENT_FACTS if f"```\n{text}\n```" in prompt]
            if len(passages) != 1:
                return self._unexpected()
            response = json.dumps({"triples": DOCUMENT_FACTS[passages[0]]})
            kind = "triples"
        else:
            return self._unexpected()
        with self._lock:
            self.calls[kind] += 1
        return response, {"fixture": True, "finish_reason": "stop"}, False

    def _unexpected(self):
        with self._lock:
            self.unexpected_calls += 1
        raise AssertionError("Unexpected synthetic model request")


class SyntheticEmbedding:
    """Small deterministic vectors, not a trained semantic embedding model."""

    query_instruction_mode = "ignored"

    def __init__(self):
        self.calls = 0
        self.text_count = 0

    def batch_encode(self, texts, **kwargs):
        if isinstance(texts, str):
            texts = [texts]
        self.calls += 1
        self.text_count += len(texts)
        vectors = []
        vocabulary = ("p101", "v20", "s7", "90", "c3", "o9", "supplies", "requires")
        for text in texts:
            words = Counter(re.findall(r"[a-z0-9]+", text.casefold().replace("mainpump", "p101")))
            vector = np.array([words[word] for word in vocabulary] + [0.05], dtype=np.float32)
            vectors.append(vector / np.linalg.norm(vector))
        return np.asarray(vectors, dtype=np.float32)


def make_engine(*, save_dir, llm=None, embedding=None):
    from hipporag import HippoRAG
    from hipporag.utils.config_utils import BaseConfig

    llm = llm or SyntheticLLM()
    embedding = embedding or SyntheticEmbedding()
    return HippoRAG(
        global_config=BaseConfig(
            save_dir=str(save_dir), llm_name="synthetic-offline-v1",
            embedding_model_name="synthetic-lexical-v1", openie_mode="online",
            openie_max_workers=1, synonymy_edge_topk=16, linking_top_k=8,
            retrieval_top_k=6, max_retry_attempts=0, save_openie=True,
        ),
        extraction_llm=llm, qa_llm=llm, embedding_model=embedding,
        index_identity="synthetic-components-v1",
    )
