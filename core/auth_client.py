import base64
import json
import re
import shutil
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from core.auth_policy import get_effective_hosts, is_ip_host, update_remote_hosts
from core.paths import app_base_dir, resource_path


AUTH_CA_FILENAMES = ("yho_auth_ca.pem", "yho_root_ca.pem")
AUTH_CHECK_INTERVAL_SECONDS = 600
AUTH_OFFLINE_GRACE_SECONDS = 0
JWT_REFRESH_LEAD_SECONDS = 300
WS_PING_INTERVAL_SECONDS = 30
WS_RECONNECT_DELAYS = (1, 2, 4, 8, 30)
DETERMINISTIC_AUTH_FAILURE_STATUSES = {
    "not_found",
    "released",
    "revoked",
    "suspended",
    "device_mismatch",
    "expired",
    "deleted",
}
AUTH_REBIND_STATUSES = {
    "not_found",
    "released",
    "expired",
    "token_already_issued",
    "missing_token",
    "invalid_token",
}
AUTH_ADMIN_BLOCKED_STATUSES = {
    "revoked",
    "suspended",
    "deleted",
}


class AuthClientError(Exception):
    pass


@dataclass
class GateDecision:
    allowed: bool
    status: str
    message: str


@dataclass
class AuthRecoveryDecision:
    mode: str
    status: str
    message: str
    can_rebind: bool = False
    can_recheck: bool = False
    admin_blocked: bool = False
    should_persist: bool = False


def _normalized_status(value):
    return str(value or "").strip().lower()


def _diagnostic_device_statuses(check_result):
    if not isinstance(check_result, dict):
        return []
    diagnostic = check_result.get("diagnostic")
    if not isinstance(diagnostic, dict):
        return []
    records = diagnostic.get("device_records")
    if not isinstance(records, list):
        return []
    statuses = []
    for item in records:
        if isinstance(item, dict):
            status = _normalized_status(item.get("status"))
            if status:
                statuses.append(status)
    return statuses


def effective_auth_status(state=None, check_result=None):
    result = check_result if isinstance(check_result, dict) else {}
    explicit = _normalized_status(result.get("effective_status"))
    if explicit:
        return explicit
    status = _normalized_status(result.get("status"))
    device_statuses = _diagnostic_device_statuses(result)
    if status == "not_found":
        for item in device_statuses:
            if item in AUTH_ADMIN_BLOCKED_STATUSES:
                return item
        for item in device_statuses:
            if item in AUTH_REBIND_STATUSES:
                return item
    if status:
        return status
    return _normalized_status(getattr(state, "status", ""))


def _default_recovery_message(mode, status, check_result=None):
    result = check_result if isinstance(check_result, dict) else {}
    raw_message = str(result.get("message") or "").strip()
    if mode == "authorized":
        return "授权有效"
    if mode == "pending_activation":
        return "绑定码已生成，请在指定 QQ 群发送 /bind 绑定码。"
    if mode == "transient_error":
        return raw_message or "授权服务器暂时不可达，请稍后重试。"
    if mode == "admin_blocked":
        return "授权已被管理员停用，不能自助重新绑定，请联系管理员处理。"
    if mode == "device_mismatch":
        return "这是其他设备的授权缓存，本机需要重新绑定。"
    if mode == "can_rebind":
        if status == "released":
            return "旧设备授权已释放，可生成新绑定码重新绑定本机。"
        if status == "expired":
            return "授权已过期，可生成新绑定码重新绑定本机。"
        if status == "token_already_issued":
            return "这个绑定码已经领取过授权，请生成新绑定码重新绑定本机。"
        return "服务器未找到旧授权记录，可能是换机/挂失释放或重绑完成；可生成新绑定码重新绑定本机。"
    return raw_message or "授权缓存过期，请联网复验。"


def classify_auth_recovery(state=None, check_result=None, network_error=False):
    result = check_result if isinstance(check_result, dict) else {}
    if network_error:
        return AuthRecoveryDecision(
            mode="transient_error",
            status="network_error",
            message=_default_recovery_message("transient_error", "network_error", result),
            can_recheck=True,
            should_persist=False,
        )
    if bool(result.get("authorized")):
        return AuthRecoveryDecision(
            mode="authorized",
            status="authorized",
            message=_default_recovery_message("authorized", "authorized", result),
            should_persist=False,
        )

    state_status = _normalized_status(getattr(state, "status", ""))
    result_status = _normalized_status(result.get("status"))
    if state_status == "pending" and getattr(state, "activation_id", "") and getattr(state, "user_code", "") and not result_status:
        return AuthRecoveryDecision(
            mode="pending_activation",
            status="pending",
            message=_default_recovery_message("pending_activation", "pending", result),
            should_persist=True,
        )
    status = effective_auth_status(state, result)
    if status in AUTH_ADMIN_BLOCKED_STATUSES:
        return AuthRecoveryDecision(
            mode="admin_blocked",
            status=status,
            message=_default_recovery_message("admin_blocked", status, result),
            admin_blocked=True,
            should_persist=True,
        )
    if status == "device_mismatch":
        return AuthRecoveryDecision(
            mode="device_mismatch",
            status=status,
            message=_default_recovery_message("device_mismatch", status, result),
            can_rebind=True,
            should_persist=True,
        )
    rebind_allowed = result.get("rebind_allowed")
    if rebind_allowed is True or status in AUTH_REBIND_STATUSES:
        return AuthRecoveryDecision(
            mode="can_rebind",
            status=status or "missing_token",
            message=_default_recovery_message("can_rebind", status, result),
            can_rebind=True,
            can_recheck=bool(getattr(state, "access_token", "")),
            should_persist=True,
        )
    if getattr(state, "access_token", ""):
        return AuthRecoveryDecision(
            mode="recheck_only",
            status=status or "needs_recheck",
            message=_default_recovery_message("recheck_only", status, result),
            can_recheck=True,
            should_persist=False,
        )
    return AuthRecoveryDecision(
        mode="can_rebind",
        status=status or "missing_token",
        message=_default_recovery_message("can_rebind", status or "missing_token", result),
        can_rebind=True,
        should_persist=False,
    )


def build_pending_activation_state(previous_state, activation_id, user_code, device_hash, message="等待 QQ 群绑定"):
    from core.auth_store import AuthState

    return AuthState(
        status="pending",
        access_token="",
        license_id="",
        device_hash=str(device_hash or ""),
        qq_user_id_hash="",
        expires_at=0.0,
        last_checked_at=0.0,
        activation_id=str(activation_id or ""),
        user_code=str(user_code or ""),
        message=str(message or "等待 QQ 群绑定"),
    )


def should_show_authorization_dialog_after_check(reason, recovery_mode, dismissed_until=0, now=None):
    mode = str(recovery_mode or "")
    if mode in ("authorized", "transient_error"):
        return False
    normalized_reason = str(reason or "scheduled")
    if normalized_reason == "scheduled":
        return False
    if normalized_reason in ("manual", "action_gate"):
        return True
    current = time.time() if now is None else float(now)
    return current >= float(dismissed_until or 0)


def _copy_ca_to_stable_path(source_path):
    source = Path(source_path)
    target = Path(app_base_dir()) / "certs" / source.name
    try:
        if source.resolve() == target.resolve():
            return str(source)
    except OSError:
        pass
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or source.stat().st_mtime > target.stat().st_mtime:
            shutil.copy2(source, target)
        if target.is_file():
            return str(target)
    except OSError:
        return str(source)
    return str(source)


def _ca_for_host(host, default_ca_path=None):
    """Return CA cert path for host. IP uses custom CA, domain uses system trust store."""
    if is_ip_host(host):
        return default_ca_path
    return None  # System trust store for domain names


def find_auth_ca_bundle(base_dir=None):
    if base_dir is not None:
        for filename in AUTH_CA_FILENAMES:
            path = Path(base_dir) / "certs" / filename
            if path.is_file():
                return str(path)
        return None

    stable_dir = Path(app_base_dir()) / "certs"
    for filename in AUTH_CA_FILENAMES:
        path = stable_dir / filename
        if path.is_file():
            return str(path)

    for filename in AUTH_CA_FILENAMES:
        path = Path(resource_path("certs", filename))
        if path.is_file():
            return _copy_ca_to_stable_path(path)
    return None


class AuthClient:
    def __init__(self, base_url, timeout=8, transport=None, ca_bundle_path=None):
        self.base_url = str(base_url or "").rstrip("/")
        self.timeout = timeout
        self.transport = transport
        self.ca_bundle_path = ca_bundle_path if ca_bundle_path is not None else find_auth_ca_bundle()
        self._ssl_context = None
        if not self.base_url:
            raise AuthClientError("授权服务器地址未配置")

    def _url(self, path):
        return f"{self.base_url}/{str(path).lstrip('/')}"

    def _get_ssl_context(self):
        if not self.ca_bundle_path:
            return None
        if not Path(self.ca_bundle_path).is_file():
            refreshed = find_auth_ca_bundle()
            self.ca_bundle_path = refreshed
            self._ssl_context = None
        if not self.ca_bundle_path or not Path(self.ca_bundle_path).is_file():
            raise AuthClientError("授权证书文件缺失，请重新解压完整程序包后再启动")
        if self._ssl_context is None:
            self._ssl_context = ssl.create_default_context(cafile=self.ca_bundle_path)
        return self._ssl_context

    def _request(self, method, path, payload=None, token=None):
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "YHoAutoFish-auth-client/1",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        body = payload or {}
        url = self._url(path)
        if self.transport is not None:
            response = self.transport(method, url, headers, body, self.timeout)
            if isinstance(response, tuple):
                status_code, data = response
            else:
                status_code, data = 200, response
            if int(status_code) < 200 or int(status_code) >= 300:
                raise AuthClientError(str(data))
            return data

        data = None if method.upper() == "GET" else json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        last_error = None
        for attempt in range(3):
            try:
                context = self._get_ssl_context()
                handlers = [urllib.request.ProxyHandler({})]
                if context is not None:
                    handlers.append(urllib.request.HTTPSHandler(context=context))
                opener = urllib.request.build_opener(*handlers)
                with opener.open(request, timeout=self.timeout) as response:
                    raw = response.read()
                    if not raw:
                        return {}
                    return json.loads(raw.decode("utf-8"))
            except AuthClientError:
                raise
            except ssl.SSLError as exc:
                last_error = exc
                message = str(exc).lower()
                if attempt < 2 and ("eof" in message or "timed out" in message):
                    time.sleep(0.35 * (attempt + 1))
                    continue
                raise AuthClientError(f"HTTPS 证书校验失败: {exc}") from exc
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="ignore")
                raise AuthClientError(detail or str(exc)) from exc
            except urllib.error.URLError as exc:
                last_error = exc
                reason = str(getattr(exc, "reason", exc)).lower()
                if attempt < 2 and ("eof" in reason or "timed out" in reason or "connection reset" in reason):
                    time.sleep(0.35 * (attempt + 1))
                    continue
                raise AuthClientError(str(exc)) from exc
            except TimeoutError as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.35 * (attempt + 1))
                    continue
                raise AuthClientError("授权服务器请求超时") from exc
        raise AuthClientError(str(last_error) if last_error else "授权服务器请求失败")

    def start_activation(self, device_hash, install_id, app_version, device_hash_v2="", hw_ids=None):
        body = {
            "device_hash": device_hash,
            "install_id": install_id,
            "app_version": app_version,
        }
        if device_hash_v2:
            body["device_hash_v2"] = device_hash_v2
        if hw_ids:
            body["hw_ids"] = hw_ids
        return self._request_with_failover("POST", "/activation/start", body)

    def poll_activation(self, activation_id, device_hash):
        return self._request_with_failover(
            "POST",
            "/activation/poll",
            {
                "activation_id": activation_id,
                "device_hash": device_hash,
            },
        )

    def check_entitlement(self, access_token, device_hash, device_hash_v2="", hw_ids=None):
        body = {"device_hash": device_hash}
        if device_hash_v2:
            body["device_hash_v2"] = device_hash_v2
        if hw_ids:
            body["hw_ids"] = hw_ids
        return self._request_with_failover("POST", "/entitlement/check", body, token=access_token)

    def get_entitlement_status(self, access_token, device_hash):
        return self._request_with_failover(
            "POST",
            "/entitlement/status",
            {"device_hash": device_hash},
            token=access_token,
        )

    def list_public_groups(self):
        return self._request("GET", "/public/groups")

    def _request_with_failover(self, method, path, payload=None, token=None):
        """Try each host in effective hosts until one succeeds. IP first for low latency."""
        last_error = None
        for host in get_effective_hosts():
            try:
                base_url = f"https://{host}/api"
                ca_path = _ca_for_host(host, self.ca_bundle_path)
                client = AuthClient(base_url, timeout=self.timeout, ca_bundle_path=ca_path)
                return client._request(method, path, payload, token)
            except AuthClientError as exc:
                last_error = exc
                continue
        raise AuthClientError(str(last_error) if last_error else "所有授权服务器均不可达")

    def refresh_access_token(self, refresh_token, device_hash_v2="", device_hash=""):
        """V1 refresh. Server may attach jwt_token in response for V1→V2 migration."""
        payload = {"refresh_token": refresh_token, "device_hash": device_hash}
        if device_hash_v2:
            payload["device_hash_v2"] = device_hash_v2
        return self._request_with_failover("POST", "/token/refresh", payload, token=refresh_token)

    def reactivate_entitlement(self, qq_user_id_hash, device_hash, device_hash_v2=""):
        """Recover an expired entitlement by proving group membership + device ownership."""
        payload = {"qq_user_id_hash": qq_user_id_hash, "device_hash": device_hash}
        if device_hash_v2:
            payload["device_hash_v2"] = device_hash_v2
        return self._request_with_failover("POST", "/entitlement/reactivate", payload)

    def check_entitlement_v2(self, jwt_token, device_hash, device_hash_v2="", hw_ids=None):
        """V2 JWT-based entitlement check."""
        body = {"device_hash": device_hash}
        if device_hash_v2:
            body["device_hash_v2"] = device_hash_v2
        if hw_ids:
            body["hw_ids"] = hw_ids
        return self._request_with_failover("POST", "/v2/entitlement/check", body, token=jwt_token)

    def refresh_token_v2(self, refresh_token, device_hash_v2="", device_hash=""):
        """V2 token rotation: refresh_token → new JWT + new refresh_token."""
        payload = {"refresh_token": refresh_token, "device_hash": device_hash}
        if device_hash_v2:
            payload["device_hash_v2"] = device_hash_v2
        return self._request_with_failover("POST", "/v2/token/refresh", payload)

    def reactivate_entitlement_v2(self, qq_user_id_hash, device_hash, device_hash_v2=""):
        """V2 reactivate: group member verify → JWT + refresh_token."""
        payload = {"qq_user_id_hash": qq_user_id_hash, "device_hash": device_hash}
        if device_hash_v2:
            payload["device_hash_v2"] = device_hash_v2
        return self._request_with_failover("POST", "/v2/entitlement/reactivate", payload)


def decode_jwt_payload(token):
    """Decode JWT payload without signature verification (client-side exp/iat read only)."""
    try:
        parts = str(token or "").split(".")
        if len(parts) != 3:
            return None
        payload_b64 = parts[1]
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding
        raw = base64.urlsafe_b64decode(payload_b64)
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def _verify_hosts_signature(hosts, signature_b64):
    """Verify ECDSA-P256-SHA256 signature of a hosts list. Returns True if valid."""
    try:
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import hashes, serialization
        from core.auth_license import get_public_key

        pubkey_pem = get_public_key()
        if not pubkey_pem:
            return False
        public_key = serialization.load_pem_public_key(pubkey_pem)
        payload_bytes = json.dumps(hosts, sort_keys=True, separators=(",", ":")).encode("utf-8")
        signature = base64.b64decode(signature_b64)
        public_key.verify(signature, payload_bytes, ec.ECDSA(hashes.SHA256()))
        return True
    except Exception:
        return False


def _process_hosts_update(response_data):
    """Process hosts update from server auth response. Returns True if hosts were updated."""
    hosts = response_data.get("hosts")
    sig = response_data.get("hosts_sig", "")
    if not hosts or not sig or not isinstance(hosts, list):
        return False
    if not _verify_hosts_signature(hosts, sig):
        return False
    update_remote_hosts(hosts)
    return True


def auth_config_required(config):
    return True


def auth_offline_grace_seconds(config):
    return AUTH_OFFLINE_GRACE_SECONDS


def auth_check_interval_seconds(config):
    return AUTH_CHECK_INTERVAL_SECONDS


def should_persist_auth_failure(status, network_error=False):
    if network_error:
        return False
    normalized = str(status or "").strip().lower()
    return normalized in DETERMINISTIC_AUTH_FAILURE_STATUSES


def decide_cached_authorization(config, state, now=None):
    if not auth_config_required(config):
        return GateDecision(True, "disabled", "来源验证未启用")
    current = time.time() if now is None else float(now)
    # V2: grace=0, no license fallback
    is_v2 = getattr(state, 'protocol_version', 1) >= 2
    grace = 0 if is_v2 else auth_offline_grace_seconds(config)
    if state is not None and state.is_usable(current, grace):
        return GateDecision(True, "authorized", "授权缓存有效")
    # Block license fallback if binding is explicitly terminated
    state_status = str(getattr(state, "status", "") or "").strip().lower()
    if state_status in ("revoked", "released", "deleted", "suspended", "group_leave"):
        return GateDecision(False, "needs_activation", "授权已停用，需要重新绑定")
    # V1 only: try license-based offline auth
    if not is_v2:
        try:
            from core.auth_license import is_license_usable, load_license
            license_blob = load_license()
            if license_blob:
                usable, status, message, expires_at, should_renew = is_license_usable(license_blob, now=current)
                if usable:
                    return GateDecision(True, "licensed", f"离线授权有效 ({message})")
        except ImportError:
            pass
    return GateDecision(False, "needs_activation", "需要完成来源验证")
