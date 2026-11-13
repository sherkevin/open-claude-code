"""Keep the complete upstream notice, not just a shorthand MIT label."""
import hashlib
from pathlib import Path


def test_upstream_mit_notice_is_complete_and_unchanged():
    server = Path(__file__).resolve().parents[1]
    license_bytes = (server / "LICENSE.HippoRAG").read_bytes()
    assert hashlib.sha256(license_bytes).hexdigest() == (
        "93b43d692b033b76129504448695bfe76ef22d18b11e51352bfab4b5622e5aaa"
    )
    notice = (server / "NOTICE.HippoRAG").read_text()
    assert "Copyright (c) 2025 OSU Natural Language Processing" in notice
    assert "d5c8329422e0a0b834a15874545cb6a74b4f9b26" in notice
    assert "GPL-2.0-or-later" in notice
