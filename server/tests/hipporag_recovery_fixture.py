"""Real pinned algorithms with synthetic models and observable recovery calls."""
import json
import os
import time
from pathlib import Path


def create_adapter(*, trace_path, hang_path):
    from hipporag import Chunk
    from app.hipporag import HippoRAGAdapter
    from hipporag_official_fixture import SyntheticLLM, SyntheticEmbedding, make_engine

    trace, hanging = Path(trace_path), Path(hang_path)

    def record(event):
        with trace.open("a", encoding="utf-8") as output:
            output.write(json.dumps({"event": event, "pid": os.getpid()}) + "\n")

    class Model(SyntheticLLM):
        def infer(self, *args, **kwargs):
            if hanging.exists():
                record("blocked_model")
                time.sleep(600)
            return super().infer(*args, **kwargs)

    def engine_factory(*, save_dir):
        record("factory")
        engine = make_engine(save_dir=save_dir, llm=Model(), embedding=SyntheticEmbedding())
        original_index = engine.index

        def index(chunks):
            record("index")
            return original_index(chunks)

        engine.index = index
        return engine

    return HippoRAGAdapter(engine_factory=engine_factory, chunk_factory=Chunk)
