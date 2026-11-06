"""One-time, user-authorized relocation; run on Tencent as root using project venv.

Preserves an old-path compatibility link and native session project aliases.
Does not print credentials, transcripts, command arguments or file contents.
"""
import hashlib
import json
import os
import pwd
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from claude_agent_sdk._internal.sessions import project_key_for_directory

OLD = Path('/data/dengkw/open-claude-code')
NEW = Path('/data/dengkw/workspace/open-claude-code')

def alive(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().split(') ', 1)[1][0] != 'Z'
    except FileNotFoundError:
        return False

def main():
    assert os.geteuid() == 0, 'Use the authorized root execution for workspace traversal'
    assert OLD.resolve() == OLD and not OLD.is_symlink(), 'Source must be the expected real project directory'
    assert NEW.parent.resolve() == Path('/data/dengkw/workspace'), 'Unexpected destination parent'
    assert not NEW.exists() and not NEW.is_symlink(), 'Refusing to overwrite destination'
    assert OLD.stat().st_dev == NEW.parent.stat().st_dev, 'Atomic same-filesystem relocation required'
    owner = OLD.stat()
    username = pwd.getpwuid(owner.st_uid).pw_name
    state = OLD / '.runtime'
    dbpath = state / 'data/app.sqlite3'
    with sqlite3.connect(dbpath) as db:
        assert db.execute("select count(*) from runs where status in ('queued','running','awaiting_approval')").fetchone()[0] == 0, 'Active tasks must finish before relocation'
    pids = json.loads((state/'processes.json').read_text())
    subprocess.run(['sudo','-n','-u',username,str(OLD/'.venv/bin/python'),str(OLD/'scripts/preview.py'),'stop'],check=True)
    deadline = time.monotonic()+25
    while True:
        remaining = [pid for pid in pids.values() if alive(pid)]
        for proc in Path('/proc').iterdir():
            if proc.name.isdigit() and int(proc.name) != os.getpid():
                try:
                    exe = os.readlink(proc/'exe')
                    if exe.startswith(str(OLD)+'/') and 'claude_agent_sdk' in exe and alive(int(proc.name)):
                        remaining.append(int(proc.name))
                except OSError:
                    pass
        if not remaining:
            break
        assert time.monotonic()<deadline, 'Owned processes did not stop; project was not moved'
        time.sleep(.2)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup_dir=state/'backups'
    backup_dir.mkdir(mode=0o700,exist_ok=True)
    os.chown(backup_dir,owner.st_uid,owner.st_gid)
    backup=backup_dir/f'pre-relocation-{stamp}.sqlite3'
    with sqlite3.connect(dbpath) as db, sqlite3.connect(backup) as target:
        assert db.execute('pragma integrity_check').fetchone()[0] == 'ok'
        db.backup(target)
        counts={table:db.execute('select count(*) from '+table).fetchone()[0] for table in ('conversations','messages','files','runs')}
        sessions=db.execute('select id,sdk_session_id from conversations where sdk_session_id is not null').fetchall()
        file_hashes={path:hashlib.sha256(Path(path).read_bytes()).hexdigest() for (path,) in db.execute('select path from files')}
    backup.chmod(0o600)
    os.chown(backup,owner.st_uid,owner.st_gid)
    previous_acl=subprocess.check_output(['getfacl','-p',str(NEW.parent)]).decode()
    # Existing service owner needs traversal only, not permission to list siblings.
    subprocess.run(['setfacl','-m',f'u:{username}:--x',str(NEW.parent)],check=True)
    os.rename(OLD,NEW)
    OLD.symlink_to(NEW,target_is_directory=True)
    os.lchown(OLD,owner.st_uid,owner.st_gid)
    aliases=[]
    for conversation_id,session_id in sessions:
        projects=NEW/'.runtime/data/sdk-config'/conversation_id/'projects'
        matches=[p for p in projects.iterdir() if p.is_dir() and (p/f'{session_id}.jsonl').is_file()] if projects.exists() else []
        assert len(matches)==1, 'Expected exactly one existing native transcript directory'
        new_key=project_key_for_directory(NEW/'.runtime/data/workspaces'/conversation_id)
        alias=projects/new_key
        if alias != matches[0]:
            assert not alias.exists() and not alias.is_symlink(), 'Refusing to replace a native project directory'
            alias.symlink_to(matches[0].name,target_is_directory=True)
            os.lchown(alias,owner.st_uid,owner.st_gid)
            aliases.append({'conversation_id':conversation_id,'old_key':matches[0].name,'new_key':new_key})
    for path,digest in file_hashes.items():
        assert hashlib.sha256(Path(path).read_bytes()).hexdigest()==digest, 'Historical file changed during relocation'
    report={'status':'moved','source':str(OLD),'destination':str(NEW),'old_path_compatibility_link':True,'counts':counts,'verified_files':len(file_hashes),'native_session_aliases':aliases,'original_workspace_acl':previous_acl,'database_backup':str(NEW/'.runtime/backups'/backup.name),'at':stamp}
    output=NEW/'.runtime/relocation-report.json'
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    output.chmod(0o600)
    os.chown(output,owner.st_uid,owner.st_gid)
    print(json.dumps({k:v for k,v in report.items() if k not in {'native_session_aliases','original_workspace_acl'}},ensure_ascii=False))
    print('Native session directory aliases:',len(aliases))

if __name__ == '__main__':
    main()
