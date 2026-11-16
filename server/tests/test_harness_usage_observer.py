"""Run pure mock-fetch Node observer protocol tests; no Harness/provider needed."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest


def test_raw_usage_observer_mock_fetch_protocol():
    node = os.getenv('HARNESS_TEST_NODE') or shutil.which('node')
    if not node or not Path(node).is_file():
        pytest.skip('Node 24+ is needed for product-owned TypeScript observer tests')
    version = subprocess.run([node, '--version'], check=True, capture_output=True, text=True).stdout.strip()
    if int(version.lstrip('v').split('.')[0]) < 24:
        pytest.skip('Node 24+ is needed for native TypeScript stripping')
    env = {k: v for k, v in os.environ.items() if not k.startswith(('AGENT_', 'ANTHROPIC_', 'OPENAI_'))}
    result = subprocess.run([node, '--test', '--test-reporter=tap', str(Path(__file__).parent / 'node/usage_observer.test.mjs')],
                            capture_output=True, text=True, timeout=45, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert '# fail 0' in result.stdout
