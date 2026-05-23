"""
ECDSA-P256 签名的离线 License 验证模块。
License 由服务端签发，客户端验证签名和有效期。
"""
import base64
import json
import os
import time
from pathlib import Path

from core.auth_store import _protect_bytes, _unprotect_bytes
from core.paths import resource_path, writable_path

LICENSE_PUBLIC_KEY_PEM = None  # Loaded lazily
LICENSE_PUBKEY_FILENAMES = ("yho_license_pubkey.pem",)
LICENSE_FILE = "auth_license.dat"
LICENSE_GRACE_HOURS = 12
LICENSE_GRACE_DAYS = LICENSE_GRACE_HOURS / 24
LICENSE_RENEW_THRESHOLD_DAYS = 3

_SECONDS_PER_DAY = 86400


def _load_public_key_pem():
    """Load ECDSA P-256 public key from certs/ directory. Returns bytes or None."""
    for fname in LICENSE_PUBKEY_FILENAMES:
        path = Path(resource_path("certs", fname))
        if path.is_file():
            try:
                return path.read_bytes()
            except Exception:
                continue
    return None


def get_public_key():
    """Lazy-load and cache the public key. Returns PEM bytes or None."""
    global LICENSE_PUBLIC_KEY_PEM
    if LICENSE_PUBLIC_KEY_PEM is None:
        LICENSE_PUBLIC_KEY_PEM = _load_public_key_pem()
    return LICENSE_PUBLIC_KEY_PEM


def verify_license_signature(payload_dict, signature_b64):
    """
    Verify ECDSA-P256-SHA256 signature of a license payload.

    payload_dict: dict — the license payload to verify
    signature_b64: str — base64-encoded ECDSA signature

    Returns True if valid, False if invalid or crypto not available.
    Raises nothing — returns False on any error.
    """
    try:
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import hashes, serialization

        pubkey_pem = get_public_key()
        if not pubkey_pem:
            return False
        public_key = serialization.load_pem_public_key(pubkey_pem)
        payload_bytes = json.dumps(
            payload_dict, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        signature = base64.b64decode(signature_b64)
        public_key.verify(signature, payload_bytes, ec.ECDSA(hashes.SHA256()))
        return True
    except Exception:
        return False


def is_license_usable(license_blob, now=None):
    """
    Check if a license blob is valid and not expired.

    license_blob: dict with "payload" and "signature" keys.
        payload should contain at least:
          - "expires_at": float (unix timestamp)
          - "device_hash_v2": str (optional, matched against current device)
    now: float — current unix timestamp (defaults to time.time())

    Returns:
        (usable: bool, status: str, message: str, expires_at: float, should_renew: bool)
    """
    now = time.time() if now is None else float(now)

    # 1. Check license_blob structure
    if not isinstance(license_blob, dict):
        return (False, "invalid", "License 数据格式无效", 0.0, False)

    payload = license_blob.get("payload")
    signature = license_blob.get("signature")
    if not isinstance(payload, dict) or not signature:
        return (False, "invalid", "License 数据结构不完整", 0.0, False)

    # 2. Verify signature
    if not verify_license_signature(payload, signature):
        return (False, "invalid", "License 签名验证失败", 0.0, False)

    # 3. Check device_hash_v2 matches (if provided)
    expected_device_hash = payload.get("device_hash_v2")
    if expected_device_hash:
        try:
            from core.auth_device import build_device_hash_v2
            current_hash = build_device_hash_v2()
            if current_hash and current_hash != expected_device_hash:
                return (False, "device_mismatch", "License 与当前设备不匹配", 0.0, False)
        except Exception:
            pass  # If device module unavailable, skip check

    # 4. Check expires_at against now
    expires_at = float(payload.get("expires_at", 0))
    if expires_at <= 0:
        return (False, "invalid", "License 缺少过期时间", 0.0, False)

    # 5. Grace period: LICENSE_GRACE_DAYS after expiry
    grace_seconds = LICENSE_GRACE_DAYS * _SECONDS_PER_DAY
    if now > expires_at + grace_seconds:
        return (
            False,
            "expired",
            "License 已过期且超出宽限期",
            expires_at,
            True,
        )

    # 6. Renew threshold: LICENSE_RENEW_THRESHOLD_DAYS before expiry
    renew_threshold_seconds = LICENSE_RENEW_THRESHOLD_DAYS * _SECONDS_PER_DAY
    should_renew = (expires_at - now) <= renew_threshold_seconds

    if now > expires_at:
        # Within grace period
        days_expired = (now - expires_at) / _SECONDS_PER_DAY
        return (
            True,
            "grace",
            f"License 已过期，宽限期剩余 {LICENSE_GRACE_DAYS - days_expired:.1f} 天",
            expires_at,
            True,
        )

    # License is valid
    days_remaining = (expires_at - now) / _SECONDS_PER_DAY
    return (
        True,
        "valid",
        f"License 有效，剩余 {days_remaining:.1f} 天",
        expires_at,
        should_renew,
    )


def _default_license_path():
    return Path(writable_path(LICENSE_FILE))


def save_license(license_blob, path=None):
    """
    Save license to DPAPI-protected file.
    Uses same envelope format as auth_store.
    """
    target = Path(path) if path is not None else _default_license_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(
        license_blob, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    protected, payload = _protect_bytes(raw)
    envelope = {
        "version": 1,
        "protected": protected,
        "payload": base64.b64encode(payload).decode("ascii"),
    }
    tmp_path = target.with_suffix(target.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(envelope, f, ensure_ascii=False, indent=2)
    os.replace(str(tmp_path), str(target))


def load_license(path=None):
    """
    Load and return license blob dict, or None if not found/invalid.
    """
    target = Path(path) if path is not None else _default_license_path()
    if not target.exists():
        return None
    try:
        with open(target, "r", encoding="utf-8") as f:
            envelope = json.load(f)
        if os.name == "nt" and not bool(envelope.get("protected", False)):
            return None
        payload = base64.b64decode(str(envelope.get("payload", "")))
        raw = _unprotect_bytes(payload, bool(envelope.get("protected", False)))
        if not raw:
            return None
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def clear_license(path=None):
    """Delete the license file."""
    target = Path(path) if path is not None else _default_license_path()
    try:
        target.unlink()
    except FileNotFoundError:
        pass
