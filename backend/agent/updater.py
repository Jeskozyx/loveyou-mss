"""
ReleaseUpdater: Fondasi pengecekan versi release, verifikasi integritas SHA-256,
dan verifikasi tanda tangan digital sebelum staging pembaruan aplikasi.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import shutil
from pathlib import Path
from typing import Any, Callable

try:
    from backend.agent.api_client import AgentApiClient
except ImportError:
    from agent.api_client import AgentApiClient


def compute_sha256(file_path: str | Path) -> str:
    """Menghitung hash SHA-256 hex lowercase untuk file paket release."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest().lower()


def verify_sha256(file_path: str | Path, expected_sha256: str) -> bool:
    """Memverifikasi integritas file terhadap checksum SHA-256 yang diharapkan."""
    expected = (expected_sha256 or "").strip().lower()
    if not expected or not Path(file_path).exists():
        return False
    actual = compute_sha256(file_path)
    return hmac.compare_digest(actual, expected)


def verify_hmac_signature(message: str, signature_hex: str, verify_key: str) -> bool:
    """Memverifikasi tanda tangan HMAC-SHA256 pada metadata/checksum paket release."""
    if not message or not signature_hex or not verify_key:
        return False
    expected = hmac.new(verify_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.lower(), signature_hex.strip().lower())


def _parse_version(ver: str) -> tuple[int, ...]:
    parts: list[int] = []
    for token in (ver or "0.0.0").strip().lstrip("vV").split("."):
        digits = "".join(c for c in token if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts or [0])


class ReleaseUpdater:
    """Memeriksa release terbaru dari Xoose API dan memverifikasi integritasnya."""

    def __init__(self, api_client: AgentApiClient, current_version: str = "1.0.0", verify_key: str = ""):
        self.api_client = api_client
        self.current_version = current_version
        self.verify_key = verify_key

    def check_update(self) -> dict[str, Any]:
        """Memeriksa apakah tersedia versi release lebih baru dari current_version."""
        resp = self.api_client.check_latest_release(self.current_version)
        rel = resp.get("release", resp.get("data", resp)) if isinstance(resp, dict) else {}
        if not isinstance(rel, dict):
            return {"update_available": False, "current_version": self.current_version}
        latest_version = str(rel.get("version", "")).strip()
        if not latest_version:
            return {"update_available": False, "current_version": self.current_version}

        has_update = _parse_version(latest_version) > _parse_version(self.current_version)
        return {
            "update_available": has_update,
            "current_version": self.current_version,
            "latest_version": latest_version,
            "artifact_url": rel.get("artifact_url") or rel.get("artifact_path", ""),
            "sha256_checksum": rel.get("sha256_checksum", ""),
            "signature": rel.get("signature", ""),
            "release_notes": rel.get("release_notes", ""),
        }

    def verify_package(
        self,
        package_path: str | Path,
        expected_sha256: str,
        signature: str = "",
        require_signature: bool = False,
    ) -> tuple[bool, str]:
        """Memastikan paket release valid secara integritas SHA-256 dan tanda tangan digital."""
        if not verify_sha256(package_path, expected_sha256):
            return False, "Verifikasi checksum SHA-256 gagal — paket ditolak."

        if require_signature and not (signature or "").strip():
            return False, "Tanda tangan digital wajib ada — paket tanpa signature ditolak."

        if self.verify_key:
            if not verify_hmac_signature(expected_sha256.strip().lower(), signature, self.verify_key):
                return False, "Verifikasi tanda tangan digital release gagal — paket ditolak."

        return True, "Paket release terverifikasi."

    def download_and_stage_update(
        self,
        release_info: dict[str, Any],
        staging_dir: str | Path,
        require_signature: bool = False,
    ) -> tuple[bool, Path | None, str]:
        """
        Mengunduh paket release dari server Xoose ke folder staging, memverifikasi integritas
        SHA-256 & tanda tangan HMAC-SHA256, dan langsung menghapus file bila tidak valid.
        """
        artifact_url = str(release_info.get("artifact_url") or release_info.get("artifact_path") or "").strip()
        expected_sha = str(release_info.get("sha256_checksum") or "").strip().lower()
        signature = str(release_info.get("signature") or "").strip()
        version = str(release_info.get("latest_version") or release_info.get("version") or "unknown").strip()

        if not artifact_url or not expected_sha:
            return False, None, "Metadata release tidak lengkap (artifact_url / sha256_checksum kosong)."

        stage_root = Path(staging_dir)
        stage_root.mkdir(parents=True, exist_ok=True)
        safe_ver = "".join(c if c.isalnum() or c in (".", "-", "_") else "_" for c in version)
        tmp_download = stage_root / f"download_{safe_ver}.tmp"
        verified_dest = stage_root / f"verified_{safe_ver}.pkg"

        try:
            self.api_client.download_file(artifact_url, str(tmp_download))
        except Exception as exc:
            if tmp_download.exists():
                tmp_download.unlink(missing_ok=True)
            return False, None, f"Gagal mengunduh paket release: {exc}"

        ok, msg = self.verify_package(
            tmp_download,
            expected_sha256=expected_sha,
            signature=signature,
            require_signature=require_signature,
        )
        if not ok:
            tmp_download.unlink(missing_ok=True)
            return False, None, msg

        os.replace(tmp_download, verified_dest)
        return True, verified_dest, f"Paket release v{version} terverifikasi dan siap dipasang."

    def apply_staged_update_with_rollback(
        self,
        staged_file: str | Path,
        target_binary: str | Path,
        health_check_fn: Callable[[Path], bool],
    ) -> tuple[bool, str]:
        """
        Menerapkan paket terverifikasi secara atomik dengan backup & rollback otomatis
        apabila post-update health check gagal.
        """
        staged = Path(staged_file)
        target = Path(target_binary)
        if not staged.exists():
            return False, "File paket staging tidak ditemukan."

        backup = target.with_name(target.name + ".bak")
        had_original = target.exists()

        try:
            if had_original:
                shutil.copy2(target, backup)

            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(staged, target)

            healthy = False
            check_error: str | None = None
            try:
                healthy = bool(health_check_fn(target))
            except Exception as exc:
                healthy = False
                check_error = str(exc)

            if not healthy:
                # Rollback otomatis ke binary backup sebelum update
                if had_original and backup.exists():
                    shutil.copy2(backup, target)
                    backup.unlink(missing_ok=True)
                elif not had_original and target.exists():
                    target.unlink(missing_ok=True)
                detail = f" ({check_error})" if check_error else ""
                return False, f"Health check pasca-update gagal{detail} — otomatis rollback ke versi sebelumnya."

            if backup.exists():
                backup.unlink(missing_ok=True)
            return True, "Pembaruan berhasil diterapkan dan lulus health check."
        except Exception as exc:
            if had_original and backup.exists():
                try:
                    shutil.copy2(backup, target)
                except Exception:
                    pass
            return False, f"Gagal menerapkan pembaruan (rollback dijalankan): {exc}"
