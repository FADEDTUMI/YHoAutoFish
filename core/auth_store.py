import base64
import hmac
import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from core.paths import writable_path


AUTH_STORE_FILE = "auth_state.dat"
AUTH_CLOCK_SKEW_WARNING_SECONDS = 5 * 60
AUTH_CLOCK_ROLLBACK_TOLERANCE_SECONDS = 60


@dataclass
class AuthState:
    status: str = "unknown"
    access_token: str = ""
    license_id: str = ""
    device_hash: str = ""
    qq_user_id_hash: str = ""
    expires_at: float = 0.0
    last_checked_at: float = 0.0
    local_checked_at: float = 0.0
    clock_skew_seconds: float = 0.0
    monotonic_checked_at: float = 0.0
    activation_id: str = ""
    user_code: str = ""
    message: str = ""
    refresh_token: str = ""
    refresh_expires_at: float = 0.0
    license_data: str = ""
    license_expires_at: float = 0.0

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            return cls()
        fields = {key: data.get(key) for key in cls.__dataclass_fields__}
        state = cls(**fields)
        state.status = str(state.status or "unknown")
        state.access_token = str(state.access_token or "")
        state.license_id = str(state.license_id or "")
        state.device_hash = str(state.device_hash or "")
        state.qq_user_id_hash = str(state.qq_user_id_hash or "")
        state.activation_id = str(state.activation_id or "")
        state.user_code = str(state.user_code or "")
        state.message = str(state.message or "")
        state.refresh_token = str(state.refresh_token or "")
        state.license_data = str(state.license_data or "")
        state.expires_at = _as_float(state.expires_at)
        state.last_checked_at = _as_float(state.last_checked_at)
        state.local_checked_at = _as_float(state.local_checked_at)
        state.clock_skew_seconds = _as_float(state.clock_skew_seconds)
        state.monotonic_checked_at = _as_float(state.monotonic_checked_at)
        state.refresh_expires_at = _as_float(state.refresh_expires_at)
        state.license_expires_at = _as_float(state.license_expires_at)
        return state

    def to_dict(self, include_runtime=True):
        data = asdict(self)
        if not include_runtime:
            data.pop("monotonic_checked_at", None)
        return data

    def apply_check_timing(self, server_time, local_time=None, monotonic_time=None):
        server = _as_float(server_time) or time.time()
        local = time.time() if local_time is None else _as_float(local_time)
        monotonic = time.monotonic() if monotonic_time is None else _as_float(monotonic_time)
        self.last_checked_at = server
        self.local_checked_at = local
        self.clock_skew_seconds = local - server
        self.monotonic_checked_at = monotonic
        return self

    def _estimated_server_now(self, current, monotonic_now=None):
        last_checked = float(self.last_checked_at or 0)
        if last_checked <= 0:
            return 0.0

        monotonic_checked = float(self.monotonic_checked_at or 0)
        if monotonic_checked > 0:
            current_monotonic = time.monotonic() if monotonic_now is None else float(monotonic_now)
            elapsed = current_monotonic - monotonic_checked
            if elapsed < -1:
                return 0.0
            return last_checked + max(0.0, elapsed)

        local_checked = float(self.local_checked_at or 0)
        if local_checked > 0:
            if current < local_checked - AUTH_CLOCK_ROLLBACK_TOLERANCE_SECONDS:
                return 0.0
            if abs(float(self.clock_skew_seconds or 0)) > AUTH_CLOCK_SKEW_WARNING_SECONDS:
                return 0.0
            return last_checked + max(0.0, current - local_checked)

        if last_checked > current + AUTH_CLOCK_ROLLBACK_TOLERANCE_SECONDS:
            return 0.0
        return current

    def is_usable(self, now=None, offline_grace_seconds=0, monotonic_now=None, allow_license_fallback=False):
        current = time.time() if now is None else float(now)
        if self.status != "authorized" or not self.access_token:
            if allow_license_fallback and self.license_data and self.license_expires_at > current:
                return True
            return False
        estimated_server_now = self._estimated_server_now(current, monotonic_now=monotonic_now)
        if estimated_server_now <= 0:
            if allow_license_fallback and self.license_data and self.license_expires_at > current:
                return True
            return False
        if self.expires_at and estimated_server_now >= float(self.expires_at):
            if allow_license_fallback and self.license_data and self.license_expires_at > current:
                return True
            return False
        grace = max(0.0, float(offline_grace_seconds or 0))
        allowed_window = grace if grace > 0 else 60.0
        result = bool(self.last_checked_at and estimated_server_now <= float(self.last_checked_at) + allowed_window)
        if not result and allow_license_fallback and self.license_data and self.license_expires_at > current:
            return True
        return result


def _as_float(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _windows_machine_guid():
    if os.name != "nt":
        return ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
            value, _value_type = winreg.QueryValueEx(key, "MachineGuid")
        return str(value).strip()
    except Exception:
        return ""


def _compute_state_hmac(payload_bytes):
    key = _windows_machine_guid().encode("utf-8") or b"default"
    return hmac.new(key, payload_bytes, hashlib.sha256).hexdigest()


def _default_store_path():
    return Path(writable_path(AUTH_STORE_FILE))


def _protect_bytes(data):
    if os.name != "nt":
        return False, data
    try:
        import win32crypt

        return True, win32crypt.CryptProtectData(data, "YHoAutoFish auth", None, None, None, 0)
    except Exception:
        return False, data


def _unprotect_bytes(data, protected):
    if not protected:
        return data
    try:
        import win32crypt

        _description, plain = win32crypt.CryptUnprotectData(data, None, None, None, 0)
        return plain
    except Exception:
        return b""


def save_auth_state(state, path=None):
    target = Path(path) if path is not None else _default_store_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(state.to_dict(include_runtime=False), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    protected, payload = _protect_bytes(raw)
    integrity = _compute_state_hmac(payload)
    envelope = {
        "version": 1,
        "protected": protected,
        "payload": base64.b64encode(payload).decode("ascii"),
        "integrity": integrity,
    }
    tmp_path = target.with_suffix(target.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as file:
        json.dump(envelope, file, ensure_ascii=False, indent=2)
    os.replace(tmp_path, target)


def load_auth_state(path=None):
    target = Path(path) if path is not None else _default_store_path()
    if not target.exists():
        return AuthState()
    try:
        with open(target, "r", encoding="utf-8") as file:
            envelope = json.load(file)
        if os.name == "nt" and not bool(envelope.get("protected", False)):
            return AuthState(status="unknown", message="本地授权缓存未受系统保护")
        payload = base64.b64decode(str(envelope.get("payload", "")))
        raw = _unprotect_bytes(payload, bool(envelope.get("protected", False)))
        if not raw:
            return AuthState(status="unknown", message="本地授权缓存无法解密")
        expected_integrity = envelope.get("integrity")
        if expected_integrity is not None:
            actual_integrity = _compute_state_hmac(payload)
            if not hmac.compare_digest(str(expected_integrity), actual_integrity):
                return AuthState(status="unknown", message="本地授权缓存完整性校验失败")
        return AuthState.from_dict(json.loads(raw.decode("utf-8")))
    except Exception as exc:
        return AuthState(status="unknown", message=f"本地授权缓存读取失败: {exc}")


def clear_auth_state(path=None):
    target = Path(path) if path is not None else _default_store_path()
    try:
        target.unlink()
    except FileNotFoundError:
        pass
