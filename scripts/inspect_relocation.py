"""Bounded relocation preflight. Does not read credentials or session text."""
import json
import os
import sqlite3
from pathlib import Path

old = Path('/data/dengkw/open-claude-code')
new = Path('/data/dengkw/workspace/open-claude-code')
for path in (old, new.parent, new):
    print(json.dumps({'path': str(path), 'exists':path.exists(), 'symlink':path.is_symlink(), 'owner':path.stat().st_uid if path.exists() else None, 'writable':os.access(path,os.W_OK)}))
db = sqlite3.connect(f'file:{old}/.runtime/data/app.sqlite3?mode=ro',uri=True)
print('active_runs',db.execute("select count(*) from runs where status in ('queued','running','awaiting_approval')").fetchone()[0])
for table in ('conversations','messages','files'):
    print(table, db.execute('select count(*) from '+table).fetchone()[0])
print('file_schema',[row[1] for row in db.execute('pragma table_info(files)')])
db.close()
folders = list((old/'.runtime/data/sdk-config').glob('*/projects/*'))
print('native_project_directory_sample',[str(p.relative_to(old)) for p in folders[:3]])
print('native_project_directory_count',len(folders))
