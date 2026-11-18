from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from app.config import Settings
from app.kaiwu_bridge import derive_secret, render_frps_config, render_visitor_config


def settings(tmp_path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        provider_settings="",
        kaiwu_control_admin_token="synthetic-control-root",
        kaiwu_control_relay_address="relay.example.com",
        kaiwu_control_relay_port=443,
        kaiwu_control_relay_bind_port=37443,
        kaiwu_control_relay_protocol="wss",
        kaiwu_control_relay_tls_enabled=True,
        kaiwu_control_relay_tls_server_name="relay.example.com",
        kaiwu_control_relay_tls_trusted_ca_file="/etc/kaiwu/relay-ca.pem",
        kaiwu_control_edge_user="edge-synthetic",
        kaiwu_edge_visitor_host="127.0.0.1",
    )


def test_wss_config_uses_private_bind_port_and_official_fields(tmp_path):
    current = settings(tmp_path)
    frps = render_frps_config(current, "synthetic-relay-token")
    visitor = render_visitor_config(
        current,
        "conn_" + "a" * 32,
        "synthetic-relay-token",
        [{
            "source_id": "src_" + "b" * 32,
            "stcp_secret": "synthetic-stcp-token",
            "visitor_host": "127.0.0.1",
            "visitor_port": 31000,
        }],
    )
    assert "bindPort = 37443" in frps
    assert "auth.token" in frps and "synthetic-relay-token" in frps
    assert "vhostWS" not in frps
    assert 'transport.protocol = "wss"' in visitor
    assert "transport.tls.enable = true" in visitor
    assert 'transport.tls.serverName = "relay.example.com"' in visitor
    assert 'transport.tls.trustedCaFile = "/etc/kaiwu/relay-ca.pem"' in visitor
    assert "bindPort = 31000" in visitor

    binary = os.getenv("KAIWU_FRPS_TEST_BINARY", "")
    if binary:
        for name, content in (("frps.toml", frps), ("visitor.toml", visitor)):
            path = tmp_path / name
            path.write_text(content, encoding="utf-8")
            command = [binary, "verify", "-c", str(path)]
            if name == "visitor.toml":
                command[0] = os.getenv("KAIWU_FRPC_TEST_BINARY", binary)
            completed = subprocess.run(command, text=True, capture_output=True)
            assert completed.returncode == 0, completed.stdout + completed.stderr


def test_secret_derivation_is_stable_and_purpose_separated(tmp_path):
    current = settings(tmp_path)
    relay = derive_secret(current, "relay", "conn_synthetic")
    stcp = derive_secret(current, "stcp", "conn_synthetic:src_synthetic:1")
    assert relay == derive_secret(current, "relay", "conn_synthetic")
    assert relay != stcp
    assert "synthetic-control-root" not in relay


def test_settings_reject_invalid_bind_port(tmp_path):
    with pytest.raises(ValueError):
        Settings(
            data_dir=tmp_path,
            provider_settings="",
            kaiwu_control_relay_bind_port=70000,
        )
