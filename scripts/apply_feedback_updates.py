"""Plan and apply the user's narrow model/configuration and assistant cleanup updates.

Default operation only writes a reviewable plan. Applying uses that exact plan;
it never grants an administrator role or removes conversations/files.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import sqlite3

VERIFIED_IMAGE_ROUTES = {
    'dano-v2.1': {'deepseek-v4.1-flash-1017'},
    'deepseek-v4.1-flash': {'deepseek-v4.1-flash'},
    'hy4': {'hy4'},
}

def image_catalog(catalog):
    result=copy.deepcopy(catalog)
    changed=[]
    for model in result.get('models',[]):
        if model.get('enabled',True) and model.get('upstream_model') in VERIFIED_IMAGE_ROUTES.get(model.get('id'),set()):
            values=model.setdefault('capabilities',[])
            if 'multimodal' not in values:
                values.append('multimodal');changed.append(model['id'])
    return result,changed

def plan_updates(db):
    db.row_factory=sqlite3.Row
    user=db.execute("SELECT id,username,role,enabled FROM users WHERE username='user'").fetchone()
    assistants=db.execute("SELECT id,name,scope,enabled,deleted,revision,editor_user_ids FROM assistants WHERE name='工业业务助手' AND scope='team' AND deleted=0").fetchall()
    if user is None or not user['enabled'] or user['role']!='member' or len(assistants)!=1:
        raise ValueError('The specifically approved member/shared assistant could not be identified uniquely')
    disabled=[dict(row) for row in db.execute('SELECT id,name,revision FROM assistants WHERE enabled=0 AND deleted=0 ORDER BY id')]
    assistant=dict(assistants[0])
    if not assistant['enabled']:raise ValueError('Industrial assistant must remain enabled')
    return {'schema':'feedback-update/v1','user':dict(user),'assistant':assistant,'disabled_assistants':disabled}

def apply_updates(db,plan):
    if plan.get('schema')!='feedback-update/v1':raise ValueError('Unsupported plan')
    db.row_factory=sqlite3.Row
    db.execute('BEGIN IMMEDIATE')
    try:
        actual=plan_updates(db)
        if actual!=plan:raise ValueError('The reviewed account/assistant state changed; create a new plan')
        editors=json.loads(plan['assistant']['editor_user_ids'] or '[]')
        if not isinstance(editors,list) or any(not isinstance(value,str) for value in editors):raise ValueError('Invalid editor list')
        editors=list(dict.fromkeys([*editors,plan['user']['id']]))
        db.execute('UPDATE assistants SET editor_user_ids=?,revision=revision+1 WHERE id=?',(json.dumps(editors),plan['assistant']['id']))
        for row in plan['disabled_assistants']:
            changed=db.execute('UPDATE assistants SET deleted=1,revision=revision+1 WHERE id=? AND enabled=0 AND deleted=0 AND revision=?',(row['id'],row['revision']))
            if changed.rowcount!=1:raise ValueError('Disabled assistant changed; transaction aborted')
        assert db.execute('SELECT role FROM users WHERE id=?',(plan['user']['id'],)).fetchone()['role']=='member'
        db.commit()
    except BaseException:
        db.rollback();raise
    return {'editor_granted_to':plan['user']['username'],'assistant_name':plan['assistant']['name'],
            'soft_deleted_disabled_assistants':len(plan['disabled_assistants']),
            'user_role_unchanged':True,'conversations_and_files_untouched':True}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path,required=True)
    parser.add_argument('--plan',type=Path,required=True)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--catalog-input',type=Path)
    parser.add_argument('--catalog-output',type=Path)
    args=parser.parse_args();os.umask(0o077)
    catalog_result=None
    if args.catalog_input or args.catalog_output:
        if not args.catalog_input or not args.catalog_output or args.catalog_input.resolve()==args.catalog_output.resolve() or args.catalog_output.exists():
            raise ValueError('Use separate input and new output catalog paths')
        catalog_result=image_catalog(json.loads(args.catalog_input.read_text(encoding='utf-8')))
        # Complete configuration I/O before any database mutation.
        with args.catalog_output.open('x',encoding='utf-8') as output:
            json.dump(catalog_result[0],output,ensure_ascii=False,indent=2)
    if args.apply:
        plan=json.loads(args.plan.read_text(encoding='utf-8'))
        receipt=args.plan.with_name(args.plan.stem+'-applied.json')
        if receipt.exists():raise ValueError('Plan already applied; do not replay')
        # Reserve the receipt before changing data. A crash leaves an explicit
        # incomplete marker, so this command can never silently replay a plan.
        with receipt.open('x',encoding='utf-8') as output:
            json.dump({'status':'started','instruction':'Inspect database state before retrying'},output)
        with sqlite3.connect(args.database) as db:result=apply_updates(db,plan)
        try:
            receipt.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        except OSError as exc:
            raise RuntimeError('Database committed but receipt update failed; inspect state and do not replay') from exc
        print(json.dumps(result,ensure_ascii=False))
    else:
        if args.plan.exists():raise ValueError('Plan exists; preserve the reviewed snapshot')
        with sqlite3.connect(f'file:{args.database.resolve().as_posix()}?mode=ro',uri=True) as db:plan=plan_updates(db)
        args.plan.parent.mkdir(parents=True,exist_ok=True)
        args.plan.write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'plan':str(args.plan),'disabled_assistant_count':len(plan['disabled_assistants']),'user_role':'member'},ensure_ascii=False))
    if catalog_result:
        print(json.dumps({'verified_image_routes_added':catalog_result[1],'other_routes_unchanged':True}))

if __name__=='__main__':main()
