"""
Milestone 7 — Pengujian Update Staging, Verifikasi SHA-256 + HMAC Signature,
dan Automatic Rollback saat Post-Update Health Check gagal.
"""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path

import httpx

from backend.agent import AgentApiClient, ReleaseUpdater


def test_download_stage_verify_and_apply_update_with_automatic_rollback(tmp_path: Path) -> None:
    verify_key = "m7-hmac-signing-secret"
    v2_bytes = b"LOVEYOU_V2_VERIFIED_BINARY_PAYLOAD"
    v2_sha = hashlib.sha256(v2_bytes).hexdigest()
    v2_sig = hmac.new(verify_key.encode("utf-8"), v2_sha.encode("utf-8"), hashlib.sha256).hexdigest()

    tampered_mode = {"tamper": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/agent/releases/1/download":
            if request.headers.get("Authorization") != "Bearer dev_tok_ok":
                return httpx.Response(401, json={"message": "Unauthorized"})
            payload = b"CORRUPTED_MALICIOUS_BYTES" if tampered_mode["tamper"] else v2_bytes
            return httpx.Response(200, content=payload)
        return httpx.Response(404)

    client = AgentApiClient("https://xoose.local", device_token="dev_tok_ok", transport=httpx.MockTransport(handler))
    updater = ReleaseUpdater(api_client=client, current_version="1.0.0", verify_key=verify_key)

    release_info = {
        "latest_version": "2.0.0",
        "artifact_url": "/api/v1/agent/releases/1/download",
        "sha256_checksum": v2_sha,
        "signature": v2_sig,
    }
    staging = tmp_path / "staging"

    # 1. File korup/tampered harus ditolak dan file temporer langsung dihapus
    tampered_mode["tamper"] = True
    ok_bad, staged_bad, msg_bad = updater.download_and_stage_update(release_info, staging, require_signature=True)
    assert ok_bad is False
    assert staged_bad is None
    assert "sha-256" in msg_bad.lower()
    assert list(staging.glob("*.tmp")) == []

    # 2. Tanda tangan kosong/salah saat require_signature=True ditolak
    tampered_mode["tamper"] = False
    bad_sig_info = dict(release_info, signature="")
    ok_nosig, staged_nosig, _ = updater.download_and_stage_update(bad_sig_info, staging, require_signature=True)
    assert ok_nosig is False
    assert staged_nosig is None

    # 3. Unduhan valid lolos SHA-256 + HMAC-SHA256 & masuk staging
    ok_good, staged_pkg, _ = updater.download_and_stage_update(release_info, staging, require_signature=True)
    assert ok_good is True
    assert staged_pkg is not None and staged_pkg.exists()

    # 4. Simulasi apply update dengan health_check GAGAL -> rollback otomatis ke binary v1 asli!
    target_bin = tmp_path / "app" / "LoveYou.exe"
    target_bin.parent.mkdir(parents=True, exist_ok=True)
    v1_bytes = b"LOVEYOU_V1_STABLE_ORIGINAL_BINARY"
    target_bin.write_bytes(v1_bytes)

    applied_fail, rollback_msg = updater.apply_staged_update_with_rollback(
        staged_pkg,
        target_bin,
        health_check_fn=lambda _p: False,
    )
    assert applied_fail is False
    assert "rollback" in rollback_msg.lower()
    assert target_bin.read_bytes() == v1_bytes  # Binary asli pulih 100%!

    # 5. Simulasi apply update dengan health_check SUKSES -> binary terpasang bersih
    applied_ok, _ = updater.apply_staged_update_with_rollback(
        staged_pkg,
        target_bin,
        health_check_fn=lambda p: p.read_bytes() == v2_bytes,
    )
    assert applied_ok is True
    assert target_bin.read_bytes() == v2_bytes
    client.close()
