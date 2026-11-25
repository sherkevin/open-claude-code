"""Rollback and restart tests; no production data or external model calls."""
from datetime import datetime,timedelta,timezone
import importlib.util
import os
from pathlib import Path
import subprocess
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.db import Database, now
from app.main import create_app
from app.conversation_schedules import enqueue_due_schedules, _queue_occurrence
from app.service import Service
from test_api import FakeRuntime,PASSWORD,login,conversation

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location("schedule_rollback_gate",ROOT/"scripts/kaiwu_schedule_rollback.py")
gate=importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def stamp(seconds=3600):
    return (datetime.now(timezone.utc)+timedelta(seconds=seconds)).isoformat()


def seed(db):
    t=now()
    db.execute("INSERT INTO users(id,username,display_name,password_hash,role,created_at) VALUES(?,?,?,?,?,?)",
               ("u","fixture","fixture","unused","member",t))
    db.execute("INSERT INTO assistants(id,name,description,instructions,enabled,access,connections) VALUES(?,?,?,?,?,?,?)",
               ("a","fixture","","",1,"private","[]"))
    db.execute("INSERT INTO conversations(id,user_id,assistant_id,title,created_at,updated_at) VALUES(?,?,?,?,?,?)",
               ("c","u","a","fixture",t,t))


def series(db,id,kind,status="scheduled",next_at=None):
    db.execute("""INSERT INTO conversation_schedule_series(id,conversation_id,text,next_at,recurrence,timezone,local_time,
               execution_mode,status,created_at,rule_expression)
               VALUES(?,?,?,?,?,'UTC','09:00:00','text',?,?,?)""",
               (id,"c","synthetic fixture",next_at or stamp(),kind,status,now(),
                "0 9 * * *" if kind=="cron" else "fixture-rule" if kind=="rrule" else None))


def test_rollback_gate_readonly_and_cas(tmp_path):
    path=tmp_path/"app.sqlite3"
    db=Database(path)
    seed(db)
    series(db,"cron","cron");series(db,"rrule","rrule")
    series(db,"unknown","future-kind");series(db,"daily","daily");series(db,"paused","cron","paused")
    before=gate.inspect(db.conn)
    assert before["quiescent"] and not before["safe_for_legacy_scheduler"] and before["unsupported_active_series"]==3
    before_rows=db.all("SELECT * FROM conversation_schedule_series ORDER BY id")
    ro=gate.open_database(path,True)
    try:
        assert gate.inspect(ro)==before
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("UPDATE conversation_schedule_series SET status='cancelled'")
    finally:ro.close()
    assert db.all("SELECT * FROM conversation_schedule_series ORDER BY id")==before_rows
    with pytest.raises(gate.GuardError,match="STALE_INSPECTION"):
        gate.prepare(db.conn,"0"*64)
    assert db.all("SELECT * FROM conversation_schedule_series ORDER BY id")==before_rows
    result=gate.prepare(db.conn,before["token"])
    assert result["paused"]==3 and result["after"]["safe_for_legacy_scheduler"]
    for row in db.all("SELECT * FROM conversation_schedule_series ORDER BY id"):
        old=next(x for x in before_rows if x["id"]==row["id"])
        if row["id"] in {"cron","rrule","unknown"}:
            assert row=={**old,"status":"paused","error":gate.PAUSE_REASON}
        else:
            assert row==old
    assert gate.prepare(db.conn,result["after"]["token"])["paused"]==0
    db.close()


def test_rollback_refuses_pending_and_undelivered_occurrences(tmp_path):
    db=Database(tmp_path/"app.sqlite3");seed(db)
    series(db,"cron","cron")
    db.execute("INSERT INTO conversation_schedules(id,conversation_id,text,scheduled_at,status,created_at,series_id) VALUES(?,?,?,?,?,?,?)",
               ("schedule-committed","c","fixture",stamp(-1),"scheduled",now(),"cron"))
    current=gate.inspect(db.conn)
    assert current["undelivered_advanced_occurrences"]==1 and not current["quiescent"]
    with pytest.raises(gate.GuardError,match="NOT_QUIESCENT"):
        gate.prepare(db.conn,current["token"])
    assert db.one("SELECT status FROM conversation_schedule_series WHERE id='cron'")["status"]=="scheduled"
    with db.transaction():
        _queue_occurrence(db,db.one("SELECT * FROM conversation_schedules WHERE id='schedule-committed'"),now())
    current=gate.inspect(db.conn)
    assert current["pending_queue"]==1 and not current["quiescent"]
    with pytest.raises(gate.GuardError,match="NOT_QUIESCENT"):
        gate.prepare(db.conn,current["token"])
    assert db.one("SELECT status FROM conversation_schedules WHERE id='schedule-committed'")["status"]=="queued"
    db.close()


def test_legacy_scheduler_after_guard_never_dispatches_advanced(tmp_path):
    source=os.getenv("KAIWU_LEGACY_SOURCE")
    if not source:pytest.skip("Exact legacy source checkout required")
    source=Path(source)
    assert subprocess.check_output(["git","-C",str(source),"rev-parse","--short","HEAD"],text=True).strip()=="320e9d7"
    assert subprocess.run(["git","-C",str(source),"diff","--quiet","HEAD","--","server/app/conversation_schedules.py"]).returncode==0
    legacy_spec=importlib.util.spec_from_file_location("app._legacy_schedule_for_gate",source/"server/app/conversation_schedules.py")
    legacy=importlib.util.module_from_spec(legacy_spec);legacy_spec.loader.exec_module(legacy)
    db=Database(tmp_path/"app.sqlite3");seed(db)
    for id,kind in (("cron","cron"),("rrule","rrule"),("daily","daily")):
        series(db,id,kind,next_at=stamp(-1))
    result=gate.prepare(db.conn,gate.inspect(db.conn)["token"])
    assert result["paused"]==2
    class FixtureService:
        def require(self,table,id):
            return db.one("SELECT * FROM "+table+" WHERE id=?",(id,))
        def usable_assistant(self,*_):return True
    service=FixtureService();service.db=db
    legacy.enqueue_due_schedules(service)
    assert db.all("SELECT series_id FROM conversation_schedules")==[{"series_id":"daily"}]
    assert db.one("SELECT count(*) n FROM message_queue")["n"]==1
    assert db.one("SELECT count(*) n FROM conversation_schedule_series WHERE recurrence IN ('cron','rrule') AND status='paused'")["n"]==2
    db.close()


def wait_run(service,cid,oid):
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        row=service.db.one("SELECT id,status FROM runs WHERE conversation_id=? AND client_request_id=?",(cid,oid))
        if row and row["status"]=="succeeded":return row["id"]
        time.sleep(.05)
    raise AssertionError("durable queued occurrence did not finish")


def test_restart_between_queue_commit_and_dispatch_keeps_single_run_and_receipt(tmp_path):
    # Seed the durable queue without starting the runtime; this represents a
    # process dying after commit, before its scheduler tick. Reopen twice.
    settings=Settings(data_dir=tmp_path,provider_settings="",origins=("http://testserver",))
    initial=Service(settings)
    initial.create_user({"username":"admin","display_name":"admin","password":PASSWORD,"role":"admin"})
    # Use an API client first to create a valid conversation and future plan.
    initial.db.close()
    app=create_app(settings,runtime_factory=FakeRuntime)
    with TestClient(app) as client:
        login(client)
        cid=conversation(client)["id"];route=f"/api/v1/conversations/{cid}/schedules"
        created=client.post(route,json={"text":"restart fixture","scheduled_at":stamp()})
        assert created.status_code==201,created.text
        oid=created.json()["id"]
        token=client.get(route).json()[0]["edit_token"]
    offline=Database(tmp_path/"app.sqlite3")
    with offline.transaction():
        row=offline.one("SELECT * FROM conversation_schedules WHERE id=?",(oid,))
        _queue_occurrence(offline,row,now())
        offline.execute("INSERT INTO conversation_schedule_run_requests VALUES(?,?,?,?,?,?)",
                        (cid,"restart-request",oid,token,oid,now()))
    offline.close()
    app2=create_app(settings,runtime_factory=FakeRuntime)
    with TestClient(app2) as client:
        login(client)
        run=wait_run(app2.state.service,cid,oid)
        body={"request_id":"restart-request","expected_token":token}
        receipt=client.post(f"{route}/{oid}/run-now",json=body)
        assert receipt.status_code==202 and receipt.json()["run_id"]==run
    app3=create_app(settings,runtime_factory=FakeRuntime)
    with TestClient(app3) as client:
        login(client)
        assert client.post(f"{route}/{oid}/run-now",json=body).json()["run_id"]==run
        service=app3.state.service
        enqueue_due_schedules(service)
        assert service.db.one("SELECT count(*) n FROM runs WHERE conversation_id=? AND client_request_id=?",(cid,oid))["n"]==1
        assert gate.inspect(service.db.conn)["safe_for_legacy_scheduler"]


def test_mixed_backlog_reserves_advanced_slot_and_uses_real_rules(tmp_path):
    """Bounded mixed backlog smoke; not a production capacity benchmark."""
    node,source=os.getenv("HARNESS_TEST_NODE"),os.getenv("HARNESS_TEST_SOURCE")
    if not node or not source:pytest.skip("Pinned official rule engine required")
    db=Database(tmp_path/"app.sqlite3");seed(db)
    # One shared synthetic user, separate conversations so per-conversation
    # queue capacity cannot distort this scheduler admission check.
    ref=datetime.now(timezone.utc).replace(second=0,microsecond=0)-timedelta(minutes=10)
    ref=ref.replace(minute=(ref.minute//5)*5)
    due=ref.isoformat()
    for i in range(30):
        cid=f"c-{i}"
        db.execute("INSERT INTO conversations(id,user_id,assistant_id,title,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                   (cid,"u","a","synthetic",now(),now()))
        kind="daily" if i<24 else "cron"
        db.execute("""INSERT INTO conversation_schedule_series(id,conversation_id,text,next_at,recurrence,timezone,
            local_time,execution_mode,status,created_at,rule_expression)
            VALUES(?,?,?,?,?,'UTC',?,'text','scheduled',?,?)""",
            (f"series-{i:02}",cid,"synthetic instruction",due,kind,ref.time().isoformat(),now(),"*/5 * * * *" if kind=="cron" else None))
    series(db,"unknown","unexpected-rule",next_at=due)
    class FixtureService:
        def require(self,table,id):
            return db.one("SELECT * FROM "+table+" WHERE id=?",(id,))
        def usable_assistant(self,*_):return True
    service=FixtureService();service.db=db
    service.settings=Settings(data_dir=tmp_path,harness_node_path=node,harness_source_dir=source,provider_settings="")
    durations=[]
    for i in range(6):
        start=time.monotonic()
        enqueue_due_schedules(service)
        durations.append(round(time.monotonic()-start,3))
        count=db.one("""SELECT count(*) n FROM conversation_schedules s JOIN conversation_schedule_series p
                         ON p.id=s.series_id WHERE p.recurrence='cron'""")["n"]
        assert count==i+1, (i,count)
        if i==0:
            assert db.one("""SELECT count(*) n FROM conversation_schedules s JOIN conversation_schedule_series p
                             ON p.id=s.series_id WHERE p.recurrence='daily'""")["n"]==20
    assert db.one("SELECT status FROM conversation_schedule_series WHERE id='unknown'")["status"]=="paused"
    assert db.one("SELECT count(*) n FROM conversation_schedules")["n"]==30
    assert db.one("SELECT count(*) n FROM message_queue")["n"]==30
    assert db.one("SELECT count(DISTINCT series_id) n FROM conversation_schedules")["n"]==30
    assert db.one("SELECT count(*) n FROM runs")["n"]==0
    print("MIXED_BACKLOG_RECEIPT",{"legacy":24,"advanced":6,"scans":6,"scan_seconds":durations,
                                  "total_seconds":round(sum(durations),3),"queued":30})
    db.close()


def test_full_old_service_starts_on_guarded_new_database(tmp_path):
    """Start actual old application on a disposable upgraded database."""
    source=os.getenv("KAIWU_LEGACY_SOURCE")
    if not source:pytest.skip("Exact legacy source checkout required")
    root=Path(source)
    assert subprocess.check_output(["git","-C",str(root),"rev-parse","--short","HEAD"],text=True).strip()=="320e9d7"
    assert subprocess.run(["git","-C",str(root),"diff","--quiet","HEAD","--","server"]).returncode==0
    settings=Settings(data_dir=tmp_path,provider_settings="",origins=("http://testserver",))
    initial=Service(settings)
    initial.create_user({"username":"admin","display_name":"admin","password":PASSWORD,"role":"admin"})
    initial.db.close()
    app=create_app(settings,runtime_factory=FakeRuntime)
    with TestClient(app) as client:
        login(client)
        cid=conversation(client)["id"]; route=f"/api/v1/conversations/{cid}/schedules"
        single=client.post(route,json={"text":"legacy startup fixture","scheduled_at":stamp()}).json()["id"]
        series_id=client.post(route,json={"text":"legacy rollback series","scheduled_at":stamp(),"recurrence":"daily"}).json()["id"]
    db=Database(tmp_path/"app.sqlite3")
    db.execute("""UPDATE conversation_schedule_series SET recurrence='cron',rule_expression='*/5 * * * *',next_at=?
                   WHERE id=?""",(stamp(-60),series_id))
    assert gate.prepare(db.conn,gate.inspect(db.conn)["token"])["paused"]==1
    db.close()
    child=r'''
import sys
from pathlib import Path
from fastapi.testclient import TestClient
from app.main import create_app
from app.config import Settings
from test_api import FakeRuntime, login
settings=Settings(data_dir=Path(sys.argv[1]),provider_settings="",origins=("http://testserver",))
app=create_app(settings,runtime_factory=FakeRuntime)
with TestClient(app) as client:
    login(client)
    data=client.get("/api/v1/conversations/"+sys.argv[2]+"/schedules")
    assert data.status_code==200,data.status_code
    rows=data.json()
    assert any(r["id"]==sys.argv[3] and r["status"]=="scheduled" for r in rows)
    assert any(r["id"]==sys.argv[4] and r["status"]=="paused" for r in rows)
    from app.conversation_schedules import enqueue_due_schedules
    enqueue_due_schedules(app.state.service)
    assert app.state.service.db.one("SELECT count(*) n FROM conversation_schedules WHERE series_id=?",(sys.argv[4],))["n"]==0
    print("EXACT_LEGACY_APPLICATION_STARTUP_PASS")
'''
    env={k:os.environ[k] for k in ("PATH","HOME","LANG","TMPDIR") if k in os.environ}
    env.update(PYTHONPATH=str(root/"server")+os.pathsep+str(root/"server/tests"),
               AGENT_KNOWLEDGE_EMBEDDING_MODEL="local-lexical-v1")
    import sys
    result=subprocess.run([sys.executable,"-c",child,str(tmp_path),cid,single,series_id],
                           cwd=root/"server",env=env,text=True,capture_output=True,timeout=45)
    assert result.returncode==0,(result.stdout,result.stderr)
    assert "EXACT_LEGACY_APPLICATION_STARTUP_PASS" in result.stdout
    app2=create_app(settings,runtime_factory=FakeRuntime)
    with TestClient(app2) as client:
        login(client)
        rows=client.get(route).json()
        assert any(r["id"]==series_id and r["status"]=="paused" and r["rule_expression"]=="*/5 * * * *" for r in rows)
        assert any(r["id"]==single and r["status"]=="scheduled" for r in rows)
        assert app2.state.service.db.one("SELECT count(*) n FROM runs")["n"]==0
