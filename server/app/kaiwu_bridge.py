"""Private FRP service configuration and lifecycle for one Kaiwu connector.

This module intentionally wraps the official frp binaries instead of
implementing another tunnel. It generates the dedicated frps configuration and
the cloud-side visitor configuration from control-plane metadata, then keeps
those processes alive until the connector is revoked.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings


class BridgeError(RuntimeError):
    pass


class BridgeReadDB:
    """Minimal read-only view of the server database for the bridge daemon."""

    def __init__(self, path: Path):
        uri = f"file:{path.resolve()}?mode=ro"
        self.conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA query_only=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")

    def one(self, query: str, values=()):
        row = self.conn.execute(query, values).fetchone()
        return dict(row) if row else None

    def all(self, query: str, values=()):
        return [dict(row) for row in self.conn.execute(query, values).fetchall()]

    def close(self) -> None:
        self.conn.close()


def derive_secret(settings: Settings, purpose: str, value: str) -> str:
    """Derive the same non-persisted relay/STCP material as the control plane."""
    import base64

    root = settings.kaiwu_control_token()
    if not root:
        raise BridgeError("控制面管理员根密钥未配置")
    digest = hmac.new(root.encode("utf-8"), f"{purpose}:{value}".encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _toml_string(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


@dataclass(frozen=True)
class BridgeMaterial:
    connector_id: str
    relay_token: str
    frps_toml: str
    visitor_toml: str
    source_ids: tuple[str, ...]
    revision: int

    @property
    def digest(self) -> str:
        payload = json.dumps(
            {
                "connector_id": self.connector_id,
                "relay_token": self.relay_token,
                "frps_toml": self.frps_toml,
                "visitor_toml": self.visitor_toml,
                "revision": self.revision,
            },
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


def render_frps_config(settings: Settings, relay_token: str, *, log_path: str | None = None) -> str:
    if not relay_token:
        raise BridgeError("缺少专属 frps 的中继令牌")
    bind_port = settings.kaiwu_control_relay_bind_port or settings.kaiwu_control_relay_port
    lines = [
        f"bindAddr = {_toml_string(settings.kaiwu_control_relay_bind_address)}",
        f"bindPort = {bind_port}",
        "",
        'auth.method = "token"',
        f"auth.token = {_toml_string(relay_token)}",
        "",
        'transport.tls.force = false',
    ]
    lines.extend([
        "",
        f"log.to = {_toml_string(log_path or './frps.log')}",
        'log.level = "warn"',
        "log.maxDays = 3",
    ])
    return "\n".join(lines) + "\n"


def render_visitor_config(
    settings: Settings,
    connector_id: str,
    relay_token: str,
    sources: list[dict[str, Any]],
    *,
    log_path: str | None = None,
) -> str:
    if not relay_token:
        raise BridgeError("缺少云端 visitor 的中继令牌")
    if not sources:
        raise BridgeError("没有可用于 visitor 的来源")
    lines = [
        f"serverAddr = {_toml_string(settings.kaiwu_control_relay_address)}",
        f"serverPort = {settings.kaiwu_control_relay_port}",
        f"user = {_toml_string(settings.kaiwu_control_edge_user)}",
        "",
        'auth.method = "token"',
        f"auth.token = {_toml_string(relay_token)}",
        "",
        f"transport.protocol = {_toml_string(settings.kaiwu_control_relay_protocol)}",
        f"transport.tls.enable = {str(settings.kaiwu_control_relay_tls_enabled).lower()}",
    ]
    if settings.kaiwu_control_relay_tls_server_name:
        lines.append(
            f"transport.tls.serverName = {_toml_string(settings.kaiwu_control_relay_tls_server_name)}"
        )
    if settings.kaiwu_control_relay_tls_trusted_ca_file:
        lines.append(
            "transport.tls.trustedCaFile = "
            f"{_toml_string(settings.kaiwu_control_relay_tls_trusted_ca_file)}"
        )
    lines.extend([
        "",
        f"log.to = {_toml_string(log_path or './visitor.log')}",
        'log.level = "warn"',
        "log.maxDays = 3",
    ])
    seen_ports: set[int] = set()
    for source in sources:
        source_id = str(source.get("source_id") or "").strip()
        secret = str(source.get("stcp_secret") or "").strip()
        host = str(source.get("visitor_host") or "").strip()
        port = int(source.get("visitor_port") or 0)
        if not source_id or not secret or not host:
            raise BridgeError("visitor 来源缺少标识、密钥或监听地址")
        if not 1 <= port <= 65535:
            raise BridgeError(f"visitor 来源 {source_id} 的监听端口无效")
        if port in seen_ports:
            raise BridgeError("visitor 监听端口重复")
        seen_ports.add(port)
        lines.extend([
            "",
            "[[visitors]]",
            f"name = {_toml_string(source_id)}",
            'type = "stcp"',
            f"serverName = {_toml_string(source_id)}",
            f"serverUser = {_toml_string(connector_id)}",
            f"secretKey = {_toml_string(secret)}",
            f"bindAddr = {_toml_string(host)}",
            f"bindPort = {port}",
        ])
    return "\n".join(lines) + "\n"


def build_bridge_material(
    db: Any,
    settings: Settings,
    connector_id: str,
    *,
    runtime_dir: Path | None = None,
) -> BridgeMaterial:
    connector = db.one("SELECT * FROM kaiwu_connectors WHERE id=?", (connector_id,))
    if not connector:
        raise BridgeError("连接器不存在")
    if connector["revoked"]:
        raise BridgeError("连接器已撤销")
    rows = db.all(
        "SELECT s.*, r.visitor_host, r.visitor_port "
        "FROM kaiwu_sources s JOIN kaiwu_edge_routes r "
        "ON r.connector_id=s.connector_id AND r.source_id=s.source_id "
        "WHERE s.connector_id=? ORDER BY s.source_id",
        (connector_id,),
    )
    if not rows:
        raise BridgeError("连接器尚无已分配 visitor 路由的来源")
    relay_token = derive_secret(settings, "relay", connector_id)
    sources = []
    for row in rows:
        sources.append({
            "source_id": row["source_id"],
            "stcp_secret": derive_secret(
                settings,
                "stcp",
                f"{connector_id}:{row['source_id']}:{row['revision']}",
            ),
            "visitor_host": row["visitor_host"],
            "visitor_port": row["visitor_port"],
        })
    log_root = runtime_dir / "logs" if runtime_dir else None
    if log_root:
        log_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    frps_toml = render_frps_config(
        settings,
        relay_token,
        log_path=str(log_root / "frps.log") if log_root else None,
    )
    visitor_toml = render_visitor_config(
        settings,
        connector_id,
        relay_token,
        sources,
        log_path=str(log_root / "visitor.log") if log_root else None,
    )
    return BridgeMaterial(
        connector_id=connector_id,
        relay_token=relay_token,
        frps_toml=frps_toml,
        visitor_toml=visitor_toml,
        source_ids=tuple(source["source_id"] for source in sources),
        revision=int(connector["revision"]),
    )


def _write_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


def _terminate(process: subprocess.Popen | None, timeout: float = 8.0) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


class BridgeSupervisor:
    """Run a dedicated frps + cloud visitor until revocation."""

    def __init__(
        self,
        db: Any,
        settings: Settings,
        connector_id: str,
        frps_binary: Path,
        frpc_binary: Path,
        runtime_dir: Path,
        poll_seconds: float = 2.0,
    ):
        self.db = db
        self.settings = settings
        self.connector_id = connector_id
        self.frps_binary = frps_binary
        self.frpc_binary = frpc_binary
        self.runtime_dir = runtime_dir
        self.poll_seconds = max(0.5, poll_seconds)
        self.frps: subprocess.Popen | None = None
        self.visitor: subprocess.Popen | None = None
        self.digest = ""
        self.stopping = False

    def _write_configs(self, material: BridgeMaterial) -> tuple[Path, Path]:
        frps_path = self.runtime_dir / "frps.toml"
        visitor_path = self.runtime_dir / "visitor.toml"
        _write_private(frps_path, material.frps_toml)
        _write_private(visitor_path, material.visitor_toml)
        return frps_path, visitor_path

    def _spawn(self, binary: Path, config: Path) -> subprocess.Popen:
        if not binary.is_file():
            raise BridgeError(f"缺少固定版本 FRP 二进制: {binary}")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        return subprocess.Popen(
            [str(binary), "-c", str(config)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=os.name != "nt",
            creationflags=flags,
        )

    def _stop(self) -> None:
        _terminate(self.visitor)
        _terminate(self.frps)
        self.visitor = None
        self.frps = None

    def _restart(self, material: BridgeMaterial) -> None:
        self._stop()
        frps_path, visitor_path = self._write_configs(material)
        self.frps = self._spawn(self.frps_binary, frps_path)
        # frps must accept the control connection before the visitor starts.
        time.sleep(0.6)
        if self.frps.poll() is not None:
            raise BridgeError("专属 frps 启动后立即退出")
        self.visitor = self._spawn(self.frpc_binary, visitor_path)
        self.digest = material.digest

    def run_forever(self) -> int:
        def stop_handler(_signum, _frame):
            self.stopping = True

        signal.signal(signal.SIGTERM, stop_handler)
        if hasattr(signal, "SIGINT"):
            signal.signal(signal.SIGINT, stop_handler)
        while not self.stopping:
            try:
                connector = self.db.one(
                    "SELECT revoked FROM kaiwu_connectors WHERE id=?",
                    (self.connector_id,),
                )
                if not connector:
                    raise BridgeError("连接器不存在")
                if connector["revoked"]:
                    self._stop()
                    for name in ("frps.toml", "visitor.toml"):
                        try:
                            (self.runtime_dir / name).unlink()
                        except FileNotFoundError:
                            pass
                    return 0
                material = build_bridge_material(
                    self.db,
                    self.settings,
                    self.connector_id,
                    runtime_dir=self.runtime_dir,
                )
                if material.digest != self.digest:
                    self._restart(material)
                elif (
                    self.frps is None
                    or self.visitor is None
                    or self.frps.poll() is not None
                    or self.visitor.poll() is not None
                ):
                    self._restart(material)
            except BridgeError as exc:
                print(f"kaiwu bridge: {exc}", file=sys.stderr, flush=True)
                self._stop()
            except Exception as exc:  # noqa: BLE001
                print(f"kaiwu bridge: {type(exc).__name__}", file=sys.stderr, flush=True)
                self._stop()
            time.sleep(self.poll_seconds)
        self._stop()
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one dedicated Kaiwu FRP bridge")
    parser.add_argument("--data-dir", required=True, help="same AGENT_DATA_DIR as the server")
    parser.add_argument("--connector-id", required=True)
    parser.add_argument("--frps", required=True)
    parser.add_argument("--frpc", required=True)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    args = parser.parse_args(argv)

    settings = Settings(data_dir=Path(args.data_dir).resolve())
    runtime_dir = Path(args.runtime_dir).resolve()
    runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = BridgeReadDB(settings.data_dir / "app.sqlite3")
    supervisor = BridgeSupervisor(
        db,
        settings,
        args.connector_id,
        Path(args.frps).resolve(),
        Path(args.frpc).resolve(),
        runtime_dir,
        poll_seconds=args.poll_seconds,
    )
    try:
        return supervisor.run_forever()
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
