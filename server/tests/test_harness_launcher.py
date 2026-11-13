import json
import os
import subprocess

from app.harness_runtime import write_launcher


def test_launcher_exec_clears_host_credentials_and_node_options(tmp_path):
    node = tmp_path / "node-bin" / "node"
    node.parent.mkdir()
    # An executable inspection fixture at the Node boundary. This verifies the
    # real execve environment, rather than inspecting generated source strings.
    node.write_text("#!/usr/bin/python3 -I\nimport json,os,sys\nprint(json.dumps({'env':dict(os.environ),'cwd':os.getcwd(),'args':sys.argv[1:]}))\n")
    node.chmod(0o700)
    private, workspace, source = [tmp_path / name for name in ("private", "workspace", "source")]
    private.mkdir()
    workspace.mkdir()
    launcher = write_launcher(private, node, source, workspace)
    hostile = {**os.environ, "HOST_SECRET": "must-not-cross", "ANTHROPIC_API_KEY": "wrong-host-key",
               "NODE_OPTIONS": "--require /tmp/untrusted.js", "DSH_HOME": "/untrusted/home",
               "PYTHONPATH": "/untrusted/modules", "HTTP_PROXY": "http://untrusted-proxy",
               "WORKSPACE_MODEL_API_KEY": "selected-fixture-key"}
    result = subprocess.run([str(launcher), "--profile", "workspace"], env=hostile, capture_output=True, text=True, check=True, timeout=10)
    inspected = json.loads(result.stdout)
    env = inspected["env"]
    assert all(key not in env for key in ("HOST_SECRET", "ANTHROPIC_API_KEY", "NODE_OPTIONS", "PYTHONPATH", "HTTP_PROXY"))
    assert env["WORKSPACE_MODEL_API_KEY"] == "selected-fixture-key"
    assert env["DSH_HOME"] == env["HOME"] == str(private)
    assert inspected["cwd"] == str(workspace)
    assert inspected["args"][-2:] == ["--profile", "workspace"]
    assert "selected-fixture-key" not in launcher.read_text()
    assert launcher.stat().st_mode & 0o777 == 0o700
