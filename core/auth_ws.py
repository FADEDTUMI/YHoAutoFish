"""WebSocket client for real-time auth revocation notifications.

Uses synchronous `websocket-client` library, natively compatible with QThread.
"""
import json
import logging
import platform
import ssl
import sys
import threading
import time

from PySide6.QtCore import QThread, Signal

log = logging.getLogger(__name__)

WS_RECONNECT_DELAYS = (1, 2, 4, 8, 30)


def _client_app_version():
    try:
        from core.version import APP_VERSION
        return APP_VERSION
    except ImportError:
        return "unknown"


def _client_env():
    try:
        os_name = platform.system().lower()
        os_ver = platform.release().replace(".", "")
        py_ver = f"{sys.version_info.major}.{sys.version_info.minor}"
        return f"{os_name}{os_ver}-py{py_ver}"
    except Exception:
        return "unknown"


def _build_ws_ssl_context(host):
    from core.auth_policy import is_ip_host
    if is_ip_host(host):
        try:
            from core.auth_client import find_auth_ca_bundle
            ca_path = find_auth_ca_bundle()
            if ca_path:
                ctx = ssl.create_default_context(cafile=ca_path)
                return ctx
        except Exception:
            pass
        return ssl.create_default_context()
    return None


class AuthWSWorker(QThread):
    """WebSocket worker for real-time revocation push from auth server."""

    revoked = Signal(str, str)
    status_changed = Signal(str)
    error = Signal(str)
    notification_received = Signal(str)
    force_disconnect_received = Signal(str)
    trigger_upgrade_received = Signal(str)

    def __init__(self, ws_url, jwt_token, license_id, parent=None):
        super().__init__(parent)
        self.ws_url = ws_url
        self.jwt_token = jwt_token
        self.license_id = license_id
        self._stop = threading.Event()

    def update_jwt(self, new_jwt):
        self.jwt_token = new_jwt

    def request_stop(self):
        self._stop.set()

    def run(self):
        import websocket
        delays = list(WS_RECONNECT_DELAYS)
        delay_idx = 0

        while not self._stop.is_set():
            if not self.jwt_token:
                self.status_changed.emit("disconnected")
                self._stop.wait(5)
                continue

            from core.auth_client import decode_jwt_payload
            payload = decode_jwt_payload(self.jwt_token)
            exp = float(payload.get("exp", 0)) if payload else 0
            remaining = exp - time.time() if exp else 0
            if not payload or remaining < 30:
                self.status_changed.emit("disconnected")
                self._stop.wait(15)
                continue

            url = f"{self.ws_url}?token={self.jwt_token}&v={_client_app_version()}&pv=2&env={_client_env()}"
            host = self.ws_url.split("://")[-1].split("/")[0].split("?")[0]
            sslopt = {}
            ssl_ctx = _build_ws_ssl_context(host)
            if ssl_ctx:
                sslopt = {"cert_reqs": ssl.CERT_REQUIRED, "ssl_version": ssl.PROTOCOL_TLS_CLIENT}

            try:
                print(f"[auth-ws] Connecting to {self.ws_url.split('?')[0]} (JWT remaining: {remaining:.0f}s)")
                ws = websocket.create_connection(
                    url,
                    timeout=15,
                    header={"User-Agent": f"YHoAutoFish/{_client_app_version()}"},
                    sslopt=sslopt,
                    host=host,
                )
                self.status_changed.emit("connected")
                delay_idx = 0
                print("[auth-ws] Connected!")

                try:
                    ws.settimeout(35)
                    while not self._stop.is_set():
                        try:
                            raw = ws.recv()
                            if not raw:
                                break
                            self._handle_message(raw)
                        except websocket.WebSocketTimeoutException:
                            # Send ping to keep alive
                            try:
                                ws.ping()
                            except Exception:
                                break
                        except websocket.WebSocketConnectionClosedException:
                            break
                finally:
                    ws.close()

            except Exception as exc:
                err = str(exc) or type(exc).__name__
                print(f"[auth-ws] Connection error: {err}")
                self.error.emit(err)

            if self._stop.is_set():
                break

            self.status_changed.emit("reconnecting")
            delay = delays[min(delay_idx, len(delays) - 1)]
            delay_idx += 1
            self._stop.wait(delay)

        self.status_changed.emit("disconnected")

    def _handle_message(self, raw):
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        event = str(data.get("event") or "")
        if event == "revoked":
            reason = str(data.get("reason") or "revoked")
            self.revoked.emit(self.license_id, reason)
        elif event == "notification":
            message = str(data.get("message") or "")
            self.notification_received.emit(message)
        elif event == "force_disconnect":
            reason = str(data.get("reason") or "admin_disconnect")
            self.force_disconnect_received.emit(reason)
            self._stop.set()
        elif event == "trigger_upgrade":
            min_version = str(data.get("min_version") or "")
            self.trigger_upgrade_received.emit(min_version)
