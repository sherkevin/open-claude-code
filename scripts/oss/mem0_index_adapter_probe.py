"""Real pinned Mem0 integration test using synthetic data only.

Uses the existing loopback-only provider and network guard. Run in the
isolated mem0ai 2.2.1 environment, never against customer data.
"""
from __future__ import annotations
import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from http.server import ThreadingHTTPServer
from importlib.metadata import version
from mem0_probe import LocalProvider, calls

sys.path.insert(0, str(Path(__file__).resolve().parents[2]/"server"))
from app.mem0_index import (TrustedScope, SourceDocument, SourceSnapshot,
    Mem0DerivedIndex, Mem0IndexConfig, Mem0IndexUnavailable,
    load_mem0_backend, text_digest, MAX_ENUMERATION_ROWS)


class Reader:
    def __init__(self, docs):
        self.docs=docs
    def load(self, scope):
        return SourceSnapshot.build(scope,self.docs)


def scope(revision, user="synthetic-owner", cid="synthetic-session"):
    return TrustedScope(user,cid,1,revision,text_digest(f"revision-{revision}"))


def docs(revision):
    return [SourceDocument(f"item-{i}.md",f"item-{i}.md",f"Approved fixture {revision}-{i}\nSecond line.",text_digest(f"Approved fixture {revision}-{i}\nSecond line.")) for i in range(100)]


def main():
    assert version("mem0ai")=="2.2.1"
    server=ThreadingHTTPServer(("127.0.0.1",0),LocalProvider)
    worker=threading.Thread(target=server.serve_forever,daemon=True)
    worker.start()
    try:
        with tempfile.TemporaryDirectory(prefix="mem0-index-") as temp:
            root=Path(temp)
            os.environ["MEM0_DIR"]=str(root/"mem0-home")
            url=f"http://127.0.0.1:{server.server_port}/v1"
            config={
                "vector_store":{"provider":"qdrant","config":{"collection_name":"adapter_fixture","path":str(root/"vectors"),"embedding_model_dims":8,"on_disk":True}},
                "embedder":{"provider":"openai","config":{"model":"fixture-embedding","api_key":"synthetic-test-only","openai_base_url":url,"embedding_dims":8}},
                "llm":{"provider":"openai","config":{"model":"fixture-llm","api_key":"synthetic-test-only","openai_base_url":url}},
                "history_db_path":str(root/"history.sqlite"),
            }
            backend=load_mem0_backend(config)
            checks=[]
            try:
                reader=Reader(docs(1))
                adapter=Mem0DerivedIndex(Mem0IndexConfig(enabled=True),backend,reader)
                a=scope(1)
                filters={"user_id":a.user_id,"agent_id":adapter.config.agent_id,"run_id":a.conversation_id}
                first=adapter.sync(a)
                assert first.added==100 and first.status=="indexed",first
                assert len(backend.memory.get_all(filters=filters)["results"])==20
                assert len(backend.get_all(filters=filters)["results"])==100
                second=adapter.sync(a)
                assert second.status=="unchanged" and second.unchanged==100 and second.added==0,second
                checks.append("100 documents, explicit enumeration, second sync idempotent")
                hits=adapter.search("approved fixture",scope=a)
                assert len(hits.hits)==5 and hits.rejected==0,hits
                assert all(hit.text in {doc.text for doc in reader.docs} for hit in hits.hits)
                checks.append("real SDK metadata and exact source text round trip")
                reader.docs=docs(2)
                b=scope(2)
                changed=adapter.sync(b)
                assert changed.added==100 and changed.deleted==100 and changed.status=="indexed",changed
                assert len(backend.get_all(filters=filters)["results"])==100
                assert adapter.sync(b).unchanged==100
                checks.append("complete generation replacement, no duplicates")
                other=scope(1,user="synthetic-other-owner")
                other_reader=Reader(docs(3)[:2])
                other_adapter=Mem0DerivedIndex(Mem0IndexConfig(enabled=True),backend,other_reader)
                assert other_adapter.sync(other).added==2
                assert all(hit.text in {doc.text for doc in reader.docs} for hit in adapter.search("fixture",scope=b).hits)
                assert len(other_adapter.search("fixture",scope=other).hits)==2
                checks.append("real scope isolation with identical embeddings")
                reader.docs=[]
                empty=scope(3)
                # A dedicated empty-snapshot synchronization reconciles all rows.
                clean=adapter.sync(empty)
                assert clean.deleted==100 and clean.status=="indexed",clean
                assert not backend.get_all(filters=filters)["results"]
                assert len(other_adapter.search("fixture",scope=other).hits)==2
                checks.append("scoped empty snapshot cleanup preserves other owner")
                assert calls["extraction"]==0
                print(json.dumps({"sdk":version("mem0ai"),"passed":len(checks),"checks":checks,"loopback_requests":calls,"enumeration_sentinel":MAX_ENUMERATION_ROWS,"quality_evaluation":False}))
            finally:
                backend.close()
                backend.memory.vector_store.client.close()
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


if __name__=="__main__":
    main()
