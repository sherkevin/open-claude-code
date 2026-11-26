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
import time
from types import SimpleNamespace
from pathlib import Path
from http.server import ThreadingHTTPServer
from importlib.metadata import version
from mem0_probe import LocalProvider, calls
from openai import APIStatusError, APITimeoutError

sys.path.insert(0, str(Path(__file__).resolve().parents[2]/"server"))
from app.mem0_index import (TrustedScope, SourceDocument, SourceSnapshot,
    Mem0DerivedIndex, Mem0IndexConfig, Mem0IndexUnavailable, Mem0IndexScopeInvalid,
    load_mem0_backend, text_digest, MAX_ENUMERATION_ROWS)


class FaultProvider(LocalProvider):
    mode = "ok"
    fault_calls = 0
    started = threading.Event()
    release = threading.Event()

    def do_POST(self):
        if type(self).mode == "ok":
            return super().do_POST()
        self.rfile.read(int(self.headers["Content-Length"]))
        type(self).fault_calls += 1
        if type(self).mode == "stall":
            type(self).started.set()
            type(self).release.wait(10)
            return
        self.send_error(503)


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
    server=ThreadingHTTPServer(("127.0.0.1",0),FaultProvider)
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
            backend=load_mem0_backend(config, io_timeout_seconds=1.0)
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
                # Actual SDK write succeeds; the source is revoked before the
                # next document can be submitted. Roll back the acknowledged
                # new row without touching another owner's rows.
                class RevocableReader(Reader):
                    revoked=False
                    def load(self, scope):
                        if self.revoked:
                            raise Mem0IndexScopeInvalid("synthetic revocation")
                        return super().load(scope)
                revoked_reader=RevocableReader(docs(4)[:2])
                class RevokeOnAdd:
                    count=0
                    def __getattr__(self, name):
                        return getattr(backend,name)
                    def add(self,*args,**kwargs):
                        response=backend.add(*args,**kwargs)
                        self.count+=1
                        revoked_reader.revoked=True
                        return response
                revoking=RevokeOnAdd()
                revoked=scope(4)
                fenced=Mem0DerivedIndex(Mem0IndexConfig(enabled=True),revoking,revoked_reader)
                try:
                    fenced.sync(revoked)
                except Mem0IndexScopeInvalid:
                    pass
                else:
                    raise AssertionError("revoked sync must fail")
                assert revoking.count==1
                assert not backend.get_all(filters=filters)["results"]
                assert len(other_adapter.search("fixture",scope=other).hits)==2
                checks.append("revocation stops next SDK write and rolls back acknowledged row")
                # Fixed transport policy: no automatic SDK retry of a
                # failed request; a stalled response holds the host slot
                # until the actual synchronous request returns.
                from app.mem0_host import Mem0Host
                for provider in (backend.memory.embedding_model, backend.memory.llm):
                    assert provider.client.timeout == 1.0
                    assert provider.client.max_retries == 0
                FaultProvider.mode="error"
                try:
                    backend.add("synthetic failure", user_id=a.user_id,
                                agent_id=adapter.config.agent_id, run_id=a.conversation_id,
                                metadata={}, infer=False)
                except APIStatusError as exc:
                    assert exc.status_code==503
                else:
                    raise AssertionError("provider error must be surfaced")
                assert FaultProvider.fault_calls==1
                assert not backend.get_all(filters=filters)["results"]
                checks.append("503 on real SDK add makes exactly one embedding request")
                FaultProvider.mode="stall"
                host=Mem0Host(SimpleNamespace(settings=SimpleNamespace(data_dir=root)),
                              Mem0IndexConfig(enabled=True), backend=backend)
                outcome=[]
                def lookup_stall():
                    started=time.monotonic()
                    try:
                        with host._slot():
                            backend.search("synthetic stall", filters=filters,
                                           top_k=1, threshold=0)
                    except Exception as exc:
                        outcome.append((type(exc).__name__, time.monotonic()-started))
                stalled=threading.Thread(target=lookup_stall)
                stalled.start()
                try:
                    assert FaultProvider.started.wait(3)
                    try:
                        with host._slot():
                            raise AssertionError("slot released before I/O returned")
                    except Mem0IndexUnavailable:
                        pass
                    stalled.join(5)
                    assert not stalled.is_alive()
                    assert len(outcome)==1 and outcome[0][0]==APITimeoutError.__name__,outcome
                    assert 0.5 <= outcome[0][1] < 5,outcome
                    assert FaultProvider.fault_calls==2
                    with host._slot():
                        pass
                finally:
                    FaultProvider.release.set()
                    stalled.join(5)
                checks.append("real stalled read times out without retry; slot held until return")
                assert calls["extraction"]==0
                # Loader-owned SDK resources close without a separate Qdrant
                # client.close() call; reopening the same local store proves
                # the on-disk lock is released and existing rows survive.
                backend.close()
                backend.close()
                assert backend.memory.embedding_model.client.is_closed()
                assert backend.memory.llm.client.is_closed()
                assert backend.memory.db is None
                try:
                    backend.get_all(filters=filters)
                except Mem0IndexUnavailable:
                    pass
                else:
                    raise AssertionError("closed backend unexpectedly admitted read")
                reopened=load_mem0_backend(config, io_timeout_seconds=1.0)
                try:
                    other_filters={"user_id":other.user_id,"agent_id":adapter.config.agent_id,"run_id":other.conversation_id}
                    assert len(reopened.get_all(filters=other_filters)["results"])==2
                finally:
                    reopened.close()
                checks.append("real SDK owned resources close idempotently; persisted Qdrant reopens")
                print(json.dumps({"sdk":version("mem0ai"),"passed":len(checks),"checks":checks,"loopback_requests":calls,"enumeration_sentinel":MAX_ENUMERATION_ROWS,"quality_evaluation":False}))
            finally:
                backend.close()
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


if __name__=="__main__":
    main()
