"""Per-user Windows launcher for the installed Web client and Tencent tunnel."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / 'install.json').read_text(encoding='utf-8'))
STATE = ROOT / 'runtime'
STATE.mkdir(exist_ok=True)
URL = f"http://127.0.0.1:{CONFIG['client_port']}"
FLAGS = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0


def read_json(url):
    try:
        with urlopen(url, timeout=2) as response:
            return json.load(response)
    except Exception:
        return None


def start_process(name, command):
    with (STATE / (name + '.log')).open('ab') as log:
        process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=log, creationflags=FLAGS)
    record = {'pid': process.pid, 'command': command}
    (STATE / (name + '.json')).write_text(json.dumps(record), encoding='utf-8')
    return process


def wait_ready(url, process, expected):
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        if process and process.poll() is not None:
            raise RuntimeError('Local component exited; inspect private runtime logs')
        value = read_json(url)
        if value and expected(value):
            return
        time.sleep(0.3)
    raise RuntimeError('Connection did not become ready; inspect private runtime logs')


def start(open_window=True):
    backend = f"http://127.0.0.1:{CONFIG['tunnel_port']}/api/v1/health/ready"
    if not read_json(backend):
        command = [CONFIG['ssh'], '-F', CONFIG['ssh_config'], '-o', 'BatchMode=yes',
                   '-o', 'StrictHostKeyChecking=yes', '-o', 'ExitOnForwardFailure=yes',
                   '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3', '-N', '-L',
                   f"127.0.0.1:{CONFIG['tunnel_port']}:127.0.0.1:18080", 'tencent']
        process = start_process('tunnel', command)
        wait_ready(backend, process, lambda value: value.get('status') == 'ready')
    health = read_json(URL + '/_client/health')
    if health and health.get('product') != 'zhixing-local-client':
        raise RuntimeError('Local client port is occupied by another service')
    if not health:
        process = start_process('client', [CONFIG['node'], str(ROOT / 'local_client_host.cjs')])
        wait_ready(URL + '/_client/health', process, lambda value: value.get('product') == 'zhixing-local-client')
    wait_ready(URL + '/api/v1/health/ready', None, lambda value: value.get('status') == 'ready')
    if open_window:
        subprocess.Popen([CONFIG['browser'], '--user-data-dir=' + str(ROOT / 'browser-profile'),
                          '--no-first-run', '--no-default-browser-check', '--disable-extensions', '--app=' + URL],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print('Local client ready: ' + URL)


def stop():
    for name in ('client', 'tunnel'):
        record_path = STATE / (name + '.json')
        if not record_path.exists():
            continue
        record = json.loads(record_path.read_text())
        script = f"Get-CimInstance Win32_Process -Filter 'ProcessId = {int(record['pid'])}' | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
        result = subprocess.run(['powershell', '-NoProfile', '-Command', script], capture_output=True, text=True, creationflags=FLAGS)
        current = json.loads(result.stdout) if result.stdout.strip() else None
        marker = str(ROOT / 'local_client_host.cjs') if name == 'client' else f"127.0.0.1:{CONFIG['tunnel_port']}:127.0.0.1:18080"
        if current and marker in (current.get('CommandLine') or ''):
            subprocess.run(['taskkill', '/PID', str(record['pid']), '/F'], check=True, stdout=subprocess.DEVNULL, creationflags=FLAGS)
    print('Stopped only the installed local client and its tunnel')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['start', 'stop', 'status'], nargs='?', default='start')
    parser.add_argument('--no-open', action='store_true')
    args = parser.parse_args()
    try:
        if args.action == 'start': start(not args.no_open)
        elif args.action == 'stop': stop()
        else: print(json.dumps({'client': read_json(URL + '/_client/health'), 'server': read_json(URL + '/api/v1/health/ready')}))
    except Exception as exc:
        (STATE / 'launcher-error.txt').write_text(type(exc).__name__ + ': ' + str(exc), encoding='utf-8')
        raise
