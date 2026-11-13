#!/usr/bin/env python3
"""Prepare the pinned Harness source with a project-local Node/pnpm toolchain."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime"
SOURCE = RUNTIME / "third_party/deepseek-harness"
TOOLCHAIN = RUNTIME / "harness-toolchain"
PIN = "fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159"
NODE_VERSION = "24.9.0"
NODE_SHA256 = "f52ec50e959d72d5c680d9731420b2661cd2a8070e94c7369b6ddfcd8b7278be"
PNPM_VERSION = "11.28.5"
PNPM_SHA512 = "SEVj0wtMKtJ8tIqpT/KzAtvOvI/2vyiBbCWJiG4UbOf3bzVPO8GkOOb16XaMMCqgpvet2CMpA81M+BQmy8FNng=="


def download(url: str, destination: Path, algorithm: str, expected: str) -> Path:
    if not destination.exists():
        print(f"Downloading {destination.name}", flush=True)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=120) as response:
            payload = response.read()
        destination.write_bytes(payload)
    digest = hashlib.new(algorithm, destination.read_bytes()).hexdigest()
    if digest != expected:
        raise RuntimeError(f"Checksum mismatch: {destination.name}")
    return destination


def environment() -> dict[str, str]:
    """Build without inherited app credentials, user npm settings, or install hooks."""
    node = TOOLCHAIN / f"node-v{NODE_VERSION}-linux-x64/bin"
    return {
        "PATH": f"{node}:{TOOLCHAIN / 'bin'}:/usr/bin:/bin",
        "TMPDIR": str(TOOLCHAIN / "tmp"),
        "XDG_CACHE_HOME": str(TOOLCHAIN / "cache"),
        "XDG_DATA_HOME": str(TOOLCHAIN / "data"),
        "XDG_CONFIG_HOME": str(TOOLCHAIN / "config"),
        "npm_config_cache": str(TOOLCHAIN / "npm-cache"),
        "npm_config_userconfig": str(TOOLCHAIN / "empty.npmrc"),
        "npm_config_globalconfig": str(TOOLCHAIN / "empty-global.npmrc"),
        "npm_config_manage_package_manager_versions": "false",
        "LEFTHOOK": "0",
        "ELECTRON_SKIP_BINARY_DOWNLOAD": "1",
        "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1",
        "CI": "true",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true", help="Install pinned source dependencies without lifecycle scripts")
    parser.add_argument("--prepare-source", action="store_true", help="Build the host lock addon and Typert artifacts needed by the isolated source profile")
    args = parser.parse_args()
    actual = subprocess.check_output(["git", "-C", str(SOURCE), "rev-parse", "HEAD"], text=True).strip()
    if actual != PIN:
        raise RuntimeError("Harness source does not match the reviewed pin")
    if subprocess.run(["git", "-C", str(SOURCE), "diff", "--quiet", "HEAD", "--"]).returncode:
        raise RuntimeError("Harness tracked source has local modifications")
    for name in ("downloads", "tmp", "cache", "data", "config", "npm-cache", "bin"):
        (TOOLCHAIN / name).mkdir(parents=True, exist_ok=True)
    for name in ("empty.npmrc", "empty-global.npmrc"):
        (TOOLCHAIN / name).touch()
    archive_name = f"node-v{NODE_VERSION}-linux-x64.tar.xz"
    archive = download(f"https://nodejs.org/dist/v{NODE_VERSION}/{archive_name}", TOOLCHAIN / "downloads" / archive_name, "sha256", NODE_SHA256)
    node = TOOLCHAIN / f"node-v{NODE_VERSION}-linux-x64/bin/node"
    node_ready = TOOLCHAIN / ".node-unpacked"
    if not node_ready.exists():
        with tarfile.open(archive) as bundle:
            # npm/npx/corepack symlinks are unnecessary: pnpm uses its pinned JS entry.
            bundle.extractall(TOOLCHAIN, members=(item for item in bundle if item.isfile() or item.isdir()), filter="data")
        node_ready.touch()
    pnpm_archive = download(f"https://registry.npmjs.org/pnpm/-/pnpm-{PNPM_VERSION}.tgz", TOOLCHAIN / "downloads" / f"pnpm-{PNPM_VERSION}.tgz", "sha512", base64.b64decode(PNPM_SHA512).hex())
    pnpm = TOOLCHAIN / f"pnpm-{PNPM_VERSION}/package/bin/pnpm.cjs"
    if not pnpm.exists():
        with tarfile.open(pnpm_archive) as bundle:
            bundle.extractall(TOOLCHAIN / f"pnpm-{PNPM_VERSION}", filter="data")
    launcher = TOOLCHAIN / "bin/pnpm"
    launcher.write_text(f'#!/bin/sh\nexec "{node}" "{pnpm}" "$@"\n')
    launcher.chmod(0o755)
    env = environment()
    subprocess.run([str(node), "--version"], env=env, check=True)
    subprocess.run([str(launcher), "--version"], env=env, check=True)
    if args.install:
        subprocess.run([str(launcher), "install", "--frozen-lockfile", "--ignore-scripts", "--store-dir", str(TOOLCHAIN / "pnpm-store"), "--reporter=append-only"], cwd=SOURCE, env=env, check=True)
    if args.prepare_source:
        loader = SOURCE / "node_modules/tsx/dist/esm/index.mjs"
        if not loader.is_file():
            raise RuntimeError("Run --install before --prepare-source")
        subprocess.run([str(node), "native/system/scripts/build.ts", "--host-addon-only"], cwd=SOURCE, env=env, check=True)
        subprocess.run([str(node), "--import", str(loader), "scripts/generate-typert.ts"], cwd=SOURCE, env=env, check=True)
    print(json.dumps({"source_commit": actual, "node": str(node), "pnpm": str(launcher), "source": str(SOURCE)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
