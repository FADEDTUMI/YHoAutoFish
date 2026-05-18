import json
import shutil
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from core.paths import app_base_dir, resource_path


AUTH_CA_FILENAMES = ("yho_auth_ca.pem", "yho_root_ca.pem")
AUTH_CHECK_INTERVAL_SECONDS = 60
AUTH_OFFLINE_GRACE_SECONDS = 5 * 60
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

    def start_activation(self, device_hash, install_id, app_version):
        return self._request(
            "POST",
            "/activation/start",
            {
                "device_hash": device_hash,
                "install_id": install_id,
                "app_version": app_version,
            },
        )

    def poll_activation(self, activation_id, device_hash):
        return self._request(
            "POST",
            "/activation/poll",
            {
                "activation_id": activation_id,
                "device_hash": device_hash,
            },
        )

    def check_entitlement(self, access_token, device_hash):
        return self._request(
            "POST",
            "/entitlement/check",
            {"device_hash": device_hash},
            token=access_token,
        )

    def get_entitlement_status(self, access_token, device_hash):
        return self._request(
            "POST",
            "/entitlement/status",
            {"device_hash": device_hash},
            token=access_token,
        )

    def list_public_groups(self):
        return self._request("GET", "/public/groups")


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
    if state is not None and state.is_usable(current, auth_offline_grace_seconds(config)):
        return GateDecision(True, "authorized", "授权缓存有效")
    return GateDecision(False, "needs_activation", "需要完成来源验证")
