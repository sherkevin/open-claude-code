import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import pytest

spec=importlib.util.spec_from_file_location('feedback_updates',Path(__file__).resolve().parents[2]/'scripts/apply_feedback_updates.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

def fixture_db():
    db=sqlite3.connect(':memory:')
    db.executescript('''CREATE TABLE users(id TEXT,username TEXT,role TEXT,enabled INTEGER);
    CREATE TABLE assistants(id TEXT,name TEXT,scope TEXT,enabled INTEGER,deleted INTEGER,revision INTEGER,editor_user_ids TEXT);
    CREATE TABLE conversations(id TEXT,assistant_id TEXT);
    INSERT INTO users VALUES('u','user','member',1);
    INSERT INTO assistants VALUES('a','工业业务助手','team',1,0,3,'[]');
    INSERT INTO assistants VALUES('d','Disabled test assistant','team',0,0,1,'[]');
    INSERT INTO assistants VALUES('p','Personal assistant','private',1,0,1,'[]');
    INSERT INTO conversations VALUES('c','d');''')
    return db

def test_exact_member_grant_soft_delete_and_history_preservation():
    db=fixture_db();plan=module.plan_updates(db);result=module.apply_updates(db,plan)
    assert result['soft_deleted_disabled_assistants']==1
    assert json.loads(db.execute("SELECT editor_user_ids FROM assistants WHERE id='a'").fetchone()[0])==['u']
    assert db.execute("SELECT role FROM users WHERE id='u'").fetchone()[0]=='member'
    assert db.execute("SELECT deleted FROM assistants WHERE id='d'").fetchone()[0]==1
    assert db.execute("SELECT deleted FROM assistants WHERE id='p'").fetchone()[0]==0
    assert db.execute('SELECT COUNT(*) FROM conversations').fetchone()[0]==1

def test_plan_change_or_replay_rolls_back_all_mutations():
    db=fixture_db();plan=module.plan_updates(db)
    db.execute("UPDATE assistants SET enabled=1 WHERE id='d'");db.commit()
    with pytest.raises(ValueError,match='state changed'):module.apply_updates(db,plan)
    assert db.execute("SELECT editor_user_ids FROM assistants WHERE id='a'").fetchone()[0]=='[]'
    db=fixture_db();plan=module.plan_updates(db);module.apply_updates(db,plan)
    with pytest.raises(ValueError,match='state changed'):module.apply_updates(db,plan)

def test_image_patch_only_exact_verified_enabled_routes():
    original={'default_model_id':'dano-v2.1','models':[
        {'id':'dano-v2.1','upstream_model':'deepseek-v4.1-flash-1017','enabled':True,'capabilities':['industrial']},
        {'id':'deepseek-v4.1-flash','upstream_model':'different-route','enabled':True,'capabilities':[]},
        {'id':'glm-5.3','upstream_model':'glm-5.3','enabled':True,'capabilities':['general']},
        {'id':'hy4','upstream_model':'hy4','enabled':False,'capabilities':[]}]}
    updated,changed=module.image_catalog(original)
    assert changed==['dano-v2.1'] and updated['default_model_id']==original['default_model_id']
    assert original['models'][0]['capabilities']==['industrial']
    assert updated['models'][1:]==original['models'][1:]
    assert module.image_catalog(updated)==(updated,[])


@pytest.mark.parametrize('case',['existing-output','invalid-json','missing-input'])
def test_catalog_preflight_error_never_changes_database(tmp_path,monkeypatch,case):
    path=tmp_path/'app.sqlite3'
    source=fixture_db()
    with sqlite3.connect(path) as db:source.backup(db)
    plan=tmp_path/'plan.json';plan.write_text(json.dumps(module.plan_updates(source)),encoding='utf-8')
    catalog=tmp_path/'catalog.json';output=tmp_path/'new-catalog.json'
    catalog.write_text('{' if case=='invalid-json' else '{"models":[]}',encoding='utf-8')
    if case=='existing-output':output.write_text('{}',encoding='utf-8')
    argv=['apply','--database',str(path),'--plan',str(plan),'--apply','--catalog-output',str(output)]
    if case!='missing-input':argv+=['--catalog-input',str(catalog)]
    monkeypatch.setattr(sys,'argv',argv)
    with pytest.raises(ValueError):module.main()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT editor_user_ids FROM assistants WHERE id='a'").fetchone()[0]=='[]'
        assert db.execute("SELECT deleted FROM assistants WHERE id='d'").fetchone()[0]==0
    assert not (tmp_path/'plan-applied.json').exists()
