"""Manage only this project's Linux preview using systemd.

First initialize the preview with preview.py. Then stop it and run this script's
install/start commands with sudo. Model credentials remain in their private file.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pwd
import shutil
import subprocess
import tempfile
from pathlib import Path

from preview import ROOT, STATE, PYTHON, environment

COMPONENTS = ("mcp", "server", "client")
PREFIX = "zhixing-" + hashlib.sha256(str(ROOT).encode()).hexdigest()[:10]
MARKER = f"# Managed by zhixing managed_preview.py; project={ROOT}\n"


def unit_name(component):
    return f"{PREFIX}-{component}.service"


def quote(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def directive_path(value):
    value = str(value)
    if any(character in value for character in ('\n', '\r', '\x00')):
        raise SystemExit("Unsupported control character in project path")
    return value.replace('%', '%%')


def render(component, username):
    account = pwd.getpwnam(username)
    if account.pw_uid == 0:
        raise SystemExit("The application must run as a non-root service account")
    order = f" {unit_name('server')}" if component == "client" else ""
    return MARKER + f'''[Unit]
Description=Zhixing project {component}
After=network.target{order}
StartLimitIntervalSec=60
StartLimitBurst=6

[Service]
Type=simple
User={username}
WorkingDirectory={directive_path(ROOT)}
ExecStart={quote(PYTHON)} {quote(ROOT / 'scripts/managed_preview.py')} run {component}
Environment=PYTHONUNBUFFERED=1
Environment={quote('HOME=' + account.pw_dir)}
EnvironmentFile=-{directive_path(STATE / 'service.env')}
Restart=on-failure
RestartSec=3
TimeoutStopSec=30
KillMode=mixed
NoNewPrivileges=true
PrivateTmp=true
UMask=0077
StandardOutput=journal
StandardError=journal
SyslogIdentifier={PREFIX}-{component}

[Install]
WantedBy=multi-user.target
'''


def install(username):
    if os.geteuid() != 0:
        raise SystemExit("Use sudo for install; runtime services remain non-root")
    needed = [PYTHON, ROOT / 'client/dist/index.html', STATE / 'nginx/nginx.conf', STATE / 'data/app.sqlite3']
    if any(not path.exists() for path in needed) or not shutil.which('nginx'):
        raise SystemExit("Initialize the project preview and its dependencies before installing services")
    definitions = {Path('/etc/systemd/system') / unit_name(component): render(component, username) for component in COMPONENTS}
    for path in definitions:
        if path.exists() and not path.read_text().startswith(MARKER):
            raise SystemExit("Refusing to overwrite a service not owned by this project")
    # Validate all definitions before changing any installed unit.
    with tempfile.TemporaryDirectory(prefix='zhixing-units-') as directory:
        staged = []
        for path, content in definitions.items():
            target = Path(directory) / path.name
            target.write_text(content)
            staged.append(str(target))
        subprocess.run(['systemd-analyze', 'verify', *staged], check=True)
    for path, content in definitions.items():
        temporary = path.with_suffix('.service.new')
        temporary.write_text(content)
        temporary.chmod(0o644)
        os.replace(temporary, path)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'enable', *(unit_name(c) for c in COMPONENTS)], check=True)
    print("Installed and enabled only:", ', '.join(unit_name(c) for c in COMPONENTS))


def process_record(component):
    import fcntl
    STATE.mkdir(exist_ok=True, mode=0o700)
    with (STATE / 'managed-processes.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        target = STATE / 'processes.json'
        current = json.loads(target.read_text()) if target.exists() else {}
        current[component] = os.getpid()
        temporary = STATE / f'processes.{os.getpid()}.new'
        temporary.write_text(json.dumps(current, indent=2))
        temporary.chmod(0o600)
        os.replace(temporary, target)


def run(component):
    if os.geteuid() == 0:
        raise SystemExit("Runtime must not run as root")
    env = environment()
    commands = {
        'server': [str(PYTHON), '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '18080'],
        'mcp': [str(PYTHON), str(ROOT / 'examples/demo_mcp.py')],
        'client': [shutil.which('nginx') or '/usr/sbin/nginx', '-p', str(STATE / 'nginx') + '/', '-c', str(STATE / 'nginx/nginx.conf'), '-g', 'daemon off;'],
    }
    command = commands[component]
    os.chdir(ROOT)
    process_record(component)
    os.execvpe(command[0], command, env)


def status():
    result = {}
    for component in COMPONENTS:
        output = subprocess.check_output(['systemctl', 'show', unit_name(component), '--property=LoadState,ActiveState,SubState,MainPID,NRestarts,User'], text=True)
        result[component] = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
    print(json.dumps(result, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['render', 'install', 'start', 'stop', 'restart', 'status', 'run'])
    parser.add_argument('component', nargs='?', choices=COMPONENTS)
    parser.add_argument('--user', default=pwd.getpwuid(ROOT.stat().st_uid).pw_name)
    args = parser.parse_args()
    if args.action == 'render':
        for component in COMPONENTS:
            print(render(component, args.user))
    elif args.action == 'install':
        install(args.user)
    elif args.action == 'run':
        if not args.component:
            parser.error('run requires a component')
        run(args.component)
    elif args.action == 'status':
        status()
    else:
        if os.geteuid() != 0:
            raise SystemExit("Use sudo to control project services")
        selected = (args.component,) if args.component else COMPONENTS
        # Only units containing this exact project's marker may be controlled.
        for component in selected:
            path = Path('/etc/systemd/system') / unit_name(component)
            if not path.is_file() or not path.read_text().startswith(MARKER):
                raise SystemExit("Project service is not installed; no control action performed")
        subprocess.run(['systemctl', args.action, *(unit_name(c) for c in selected)], check=True)
        status()


if __name__ == '__main__':
    main()
