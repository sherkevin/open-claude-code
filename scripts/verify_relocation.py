"""Verify this development relocation using existing synthetic acceptance data."""
import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
import httpx

ROOT=Path('/data/dengkw/workspace/open-claude-code')
OLD=Path('/data/dengkw/open-claude-code')

def main():
    assert ROOT.resolve()==ROOT and OLD.is_symlink() and OLD.resolve()==ROOT
    db=sqlite3.connect(f'file:{ROOT}/.runtime/data/app.sqlite3?mode=ro',uri=True)
    db.row_factory=sqlite3.Row
    conversation=dict(db.execute("select * from conversations where title='测评：context-recall-synthetic' and archived=0 order by created_at desc limit 1").fetchone())
    sdk_id=conversation['sdk_session_id']
    assert sdk_id
    with httpx.Client(base_url='http://127.0.0.1:18080/api/v1/',headers={'Origin':'http://127.0.0.1:18081'},timeout=30) as client:
        def api(method,path,**kwargs):
            response=client.request(method,path,**kwargs)
            response.raise_for_status()
            return response.json()
        user=api('POST','auth/login',json={'username':'admin','password':(ROOT/'.runtime/admin-password').read_text().strip()})
        verified=0
        for row in db.execute('select id,path from files where user_id=?',(user['id'],)):
            response=client.get(f"files/{row['id']}/content")
            response.raise_for_status()
            assert hashlib.sha256(response.content).digest()==hashlib.sha256(Path(row['path']).read_bytes()).digest()
            verified+=1
        assert verified>0
        run_id=api('POST',f"conversations/{conversation['id']}/messages",json={'client_request_id':str(uuid.uuid4()),'text':'我们此前谈到的合成订单编号、最新数量和城市分别是什么？只根据原对话回答，不要调用工具。','file_ids':[]})['run_id']
        deadline=time.monotonic()+180
        while True:
            run=api('GET',f'runs/{run_id}')
            for approval in run.get('approvals',[]):
                if approval['status']=='pending':
                    api('POST',f"approvals/{approval['id']}/decision",json={'decision':'deny'})
                    raise AssertionError('Unexpected tool approval during synthetic recall')
            if run['status'] in {'succeeded','failed','cancelled','interrupted'}:
                break
            assert time.monotonic()<deadline,'Model response timed out'
            time.sleep(.3)
        assert run['status']=='succeeded', 'Historical session failed to resume'
        answer='\n'.join(row['text'] for row in api('GET',f"conversations/{conversation['id']}/messages") if row.get('run_id')==run_id and row['role']=='assistant')
        assert all(value in answer for value in ['TEST-042','21','苏州']), 'Historical context was not recalled'
        assert db.execute('select sdk_session_id from conversations where id=?',(conversation['id'],)).fetchone()[0]==sdk_id
        projects=ROOT/'.runtime/data/sdk-config'/conversation['id']/'projects'
        paths=[p/f'{sdk_id}.jsonl' for p in projects.iterdir() if (p/f'{sdk_id}.jsonl').is_file()]
        assert len(paths)==2 and paths[0].samefile(paths[1]), 'Native history alias does not refer to the same transcript'
        report={'status':'passed','destination':str(ROOT),'health':api('GET','health/ready'),'historical_files_downloaded_and_verified':verified,'old_session_recalled':True,'same_sdk_session':True,'single_transcript_via_alias':True,'run_id':run_id,'answer_sha256':hashlib.sha256(answer.encode()).hexdigest()}
        (ROOT/'.runtime/relocation-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps(report,ensure_ascii=False))
    db.close()

if __name__=='__main__':
    main()
