"""WebSocket client for real-time auth revocation notifications.

Uses synchronous `websocket-client` library, natively compatible with QThread.
"""
import json
import logging
import ssl
import threading
import time

from PySide6.QtCore import QThread, Signal

log = logging.getLogger(__name__)

WS_RECONNECT_DELAYS = (1, 2, 4, 8, 30)


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

            url = f"{self.ws_url}?token={self.jwt_token}"
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
                    header={"User-Agent": "YHoAutoFish/1.4.0"},
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
