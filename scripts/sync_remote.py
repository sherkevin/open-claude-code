"""Mirror source to the authorized Tencent project without deleting remote files."""
from __future__ import annotations
import io
import os
import argparse
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = "/data/dengkw/workspace/open-claude-code"
EXCLUDE = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "data", ".local", ".runtime", "test-results", "playwright-report"}
# Public, non-customer fixtures are part of the application contract: the
# remote preview must be able to reproduce the same retrieval and acceptance
# examples as school cloud. Runtime state and customer data remain excluded.
PROJECT_DIRECTORIES = {"client", "server", "docs", "examples", "scripts", "fixtures"}
PROJECT_FILES = {"README.md", "TODO.md", ".env.example", ".gitignore", "compose.yaml"}

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--with-client-build", action="store_true")
    args = parser.parse_args()
    print("Preparing source archive (dependency/data directories excluded).", flush=True)
    files = []
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for directory, subdirs, names in os.walk(ROOT):
            subdirs[:] = sorted(d for d in subdirs if (d not in EXCLUDE or (d == "dist" and args.with_client_build and Path(directory) == ROOT / "client")) and not d.startswith('.test-') and not d.endswith(".egg-info"))
            if Path(directory) == ROOT:
                subdirs[:] = [d for d in subdirs if d in PROJECT_DIRECTORIES]
                names = [n for n in names if n in PROJECT_FILES]
            for name in sorted(names):
                path = Path(directory) / name
                relative = path.relative_to(ROOT)
                if path.name in {".env", ".env.local"} or path.suffix in {".pyc", ".sqlite", ".db", ".log"}:
                    continue
                archive.add(path, arcname=str(relative).replace("\\", "/"))
                files.append(str(relative))
    print(f"Transferring {len(files)} files ({len(buffer.getvalue())} compressed bytes).", flush=True)
    subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "tencent",
         f"mkdir -p {TARGET} && tar xzf - -C {TARGET}"],
        input=buffer.getvalue(), check=True, timeout=90,
    )
    print(f"Synchronized {len(files)} source files to {TARGET}; no remote deletion.")

if __name__ == "__main__":
    main()
