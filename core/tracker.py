"""Client-side analytics tracking SDK.

Dual-channel event dispatch:
  - UI behavior events → Umami (self-hosted)
  - Domain data (fishing, perf, errors) → auth server via WS

Zero external dependencies. Thread-safe. <2% CPU overhead target.
Events persist to local SQLite — survives crashes and disconnections.
"""
import json
import logging
import os
import platform
import sqlite3
import sys
import threading
import time
import uuid
from typing import Optional
from urllib.request import Request, urlopen

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Event Buffer — SQLite-backed persistent queue (survives crashes)
# ---------------------------------------------------------------------------


class EventBuffer:
    __slots__ = ("_db_path", "_lock")

    def __init__(self, db_path: str = ""):
        if not db_path:
            try:
                from core.paths import writable_path
                db_path = writable_path("data", "analytics_queue.db")
            except Exception:
                db_path = os.path.join(os.path.expanduser("~"), ".yhofish_analytics.db")
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        os.makedirs(os.path.dirname(self._db_path) or ".", exist_ok=True)
        with self._get_conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("""CREATE TABLE IF NOT EXISTS queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                sent INTEGER DEFAULT 0
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_queue_sent ON queue(sent, id)")
            # Clean up events older than 7 days
            cutoff = time.time() - 7 * 86400
            conn.execute("DELETE FROM queue WHERE created_at < ? AND sent = 1", (cutoff,))

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def push(self, event: dict) -> bool:
        try:
            with self._lock:
                with self._get_conn() as conn:
                    conn.execute(
                        "INSERT INTO queue (event_json, created_at) VALUES (?, ?)",
                        (json.dumps(event, ensure_ascii=False), time.time()),
                    )
            return True
        except Exception as exc:
            log.debug("EventBuffer.push failed: %s", exc)
            return False

    def drain(self, batch_size: int = 100) -> list:
        with self._lock:
            with self._get_conn() as conn:
                rows = conn.execute(
                    "SELECT id, event_json FROM queue WHERE sent=0 ORDER BY id LIMIT ?",
                    (batch_size,),
                ).fetchall()
                return [{"_qid": r["id"], **json.loads(r["event_json"])} for r in rows]

    def mark_sent(self, qids: list):
        if not qids:
            return
        with self._lock:
            with self._get_conn() as conn:
                placeholders = ",".join("?" * len(qids))
                conn.execute(f"UPDATE queue SET sent=1 WHERE id IN ({placeholders})", qids)

    def push_back(self, events: list):
        # Events already in DB with sent=0, no action needed
        pass

    def size(self) -> int:
        with self._lock:
            with self._get_conn() as conn:
                return conn.execute("SELECT COUNT(*) FROM queue WHERE sent=0").fetchone()[0]


# ---------------------------------------------------------------------------
# Umami Bridge — sends UI events to self-hosted Umami
# ---------------------------------------------------------------------------


class UmamiBridge:
    __slots__ = ("_base_url", "_website_id", "_session_id")

    def __init__(self, base_url: str, website_id: str):
        self._base_url = base_url.rstrip("/")
        self._website_id = website_id
        self._session_id = str(uuid.uuid4())

    def identify(self, license_id: str):
        self._session_id = license_id

    def track(self, event_name: str, data: Optional[dict] = None):
        try:
            payload = {
                "type": "event",
                "payload": {
                    "website": self._website_id,
                    "hostname": "",
                    "language": "zh-CN",
                    "url": f"/app/{event_name}",
                    "name": event_name,
                    "data": data or {},
                },
            }
            body = json.dumps(payload).encode("utf-8")
            req = Request(
                f"{self._base_url}/api/send",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urlopen(req, timeout=5)
        except Exception as exc:
            log.debug("Umami send failed: %s", exc)


# ---------------------------------------------------------------------------
# Performance Sampler — lightweight metrics collection
# ---------------------------------------------------------------------------


class PerfSampler:
    __slots__ = (
        "_fps_samples", "_latency_samples", "_memory_samples",
        "_lock", "_last_memory_ts",
    )

    def __init__(self):
        self._fps_samples: list = []
        self._latency_samples: dict[str, list] = {}
        self._memory_samples: list = []
        self._lock = threading.Lock()
        self._last_memory_ts: float = 0.0

    def sample_fps(self, fps: float):
        with self._lock:
            self._fps_samples.append(fps)

    def sample_latency(self, category: str, ms: float):
        with self._lock:
            self._latency_samples.setdefault(category, []).append(ms)

    def sample_memory(self) -> None:
        now = time.monotonic()
        with self._lock:
            if now - self._last_memory_ts < 25:
                return
            self._last_memory_ts = now
        try:
            mem_mb = _get_process_memory_mb()
            if mem_mb > 0:
                with self._lock:
                    self._memory_samples.append(mem_mb)
        except Exception:
            pass

    def get_and_reset(self) -> dict:
        with self._lock:
            data = {}
            if self._fps_samples:
                data["fps"] = list(self._fps_samples)
                self._fps_samples.clear()
            if self._latency_samples:
                data["latency"] = {k: list(v) for k, v in self._latency_samples.items()}
                self._latency_samples.clear()
            if self._memory_samples:
                data["memory_mb"] = list(self._memory_samples)
                self._memory_samples.clear()
            return data


def _get_process_memory_mb() -> float:
    """Get current process RSS in MB via Win32 API (no psutil dependency)."""
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(counters)
        if ctypes.windll.psapi.GetProcessMemoryInfo(
            ctypes.windll.kernel32.GetCurrentProcess(),
            ctypes.byref(counters),
            counters.cb,
        ):
            return counters.WorkingSetSize / (1024 * 1024)
    except Exception:
        pass
    return 0.0


# ---------------------------------------------------------------------------
# Batch Uploader — timer-based flush every 60 seconds
# ---------------------------------------------------------------------------


class BatchUploader:
    __slots__ = ("_buffer", "_ws_send_fn", "_umami", "_interval", "_timer", "_stop", "_tracker")

    def __init__(self, buffer: EventBuffer, ws_send_fn=None, umami: UmamiBridge = None, interval: float = 60.0, tracker=None):
        self._buffer = buffer
        self._ws_send_fn = ws_send_fn
        self._umami = umami
        self._interval = interval
        self._timer: Optional[threading.Timer] = None
        self._stop = threading.Event()
        self._tracker = tracker

    def start(self):
        self._stop.clear()
        self._schedule()

    def stop(self):
        self._stop.set()
        self._flush()  # flush final batch (including session_end)
        if self._timer:
            self._timer.cancel()
            self._timer = None

    def update_ws_fn(self, fn):
        old_fn = self._ws_send_fn
        self._ws_send_fn = fn
        # Trigger immediate flush when WS reconnects (None -> valid function)
        if old_fn is None and fn is not None and not self._stop.is_set():
            threading.Thread(target=self._flush, daemon=True).start()

    def _schedule(self):
        if self._stop.is_set():
            return
        self._timer = threading.Timer(self._interval, self._flush)
        self._timer.daemon = True
        self._timer.start()

    def _flush(self):
        # NOTE: When called from stop(), _stop is already set.
        # We still flush data but skip reschedule via the finally guard.
        try:
            # Snapshot _ws_send_fn to local var to prevent TOCTOU race
            ws_fn = self._ws_send_fn

            events = self._buffer.drain()
            if not events:
                # Even with no events, flush perf samples periodically
                if self._tracker:
                    self._tracker.flush_perf()
                return

            qids = [e.pop("_qid") for e in events]
            ui_events = [e for e in events if e.get("event_type") == "ui"]
            domain_events = [e for e in events if e.get("event_type") != "ui"]

            sent_ids = []
            if ui_events and self._umami:
                for ev in ui_events:
                    self._umami.track(ev.get("event_name", "unknown"), ev.get("payload"))
                sent_ids.extend(qids[:len(ui_events)])

            if domain_events and ws_fn:
                try:
                    msg = json.dumps(
                        {"type": "analytics_batch", "events": domain_events},
                        ensure_ascii=False,
                    )
                    ws_fn(msg)
                    sent_ids.extend(qids[len(ui_events):])
                except Exception:
                    log.debug("WS send failed, %d events remain in queue", len(domain_events))

            if sent_ids:
                self._buffer.mark_sent(sent_ids)

            # Aggregate perf samples into a single snapshot event
            if self._tracker:
                self._tracker.flush_perf()
        except Exception as exc:
            log.debug("BatchUploader flush error: %s", exc)
        finally:
            # Only reschedule if not stopped
            if not self._stop.is_set():
                self._schedule()


# ---------------------------------------------------------------------------
# Event Tracker — singleton public interface
# ---------------------------------------------------------------------------


def _client_env() -> str:
    try:
        os_name = platform.system().lower()
        os_ver = platform.release().replace(".", "")
        py_ver = f"{sys.version_info.major}{sys.version_info.minor}"
        return f"{os_name}{os_ver}py{py_ver}"
    except Exception:
        return "unknown"


class EventTracker:
    _instance: Optional["EventTracker"] = None
    _init_lock = threading.Lock()

    def __init__(self):
        self._buffer = EventBuffer()
        self._perf = PerfSampler()
        self._uploader: Optional[BatchUploader] = None
        self._umami: Optional[UmamiBridge] = None
        self._license_id: str = ""
        self._app_version: str = ""
        self._client_env: str = _client_env()
        self._session_id: str = ""
        self._session_start_ts: float = 0
        self._fish_caught: int = 0
        self._fish_failed: int = 0
        self._initialized: bool = False
        self._last_sync_ts: float = 0

    @classmethod
    def get(cls) -> "EventTracker":
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def init(self, license_id: str, ws_send_fn=None, app_version: str = "",
             umami_base_url: str = "", umami_website_id: str = ""):
        self._license_id = license_id
        self._app_version = app_version
        self._session_id = str(uuid.uuid4())

        if umami_base_url and umami_website_id:
            self._umami = UmamiBridge(umami_base_url, umami_website_id)
            self._umami.identify(license_id)

        self._uploader = BatchUploader(
            self._buffer, ws_send_fn=ws_send_fn, umami=self._umami, tracker=self,
        )
        self._uploader.start()
        self._initialized = True

    def shutdown(self):
        if self._uploader:
            self._uploader.stop()
            self._uploader = None
        self._initialized = False

    def update_ws_fn(self, fn):
        if self._uploader:
            self._uploader.update_ws_fn(fn)

    # -- internal helpers --

    def _emit(self, event_type: str, event_name: str, payload: dict):
        if not self._initialized:
            return
        self._buffer.push({
            "event_id": uuid.uuid4().hex[:16],
            "event_type": event_type,
            "event_name": event_name,
            "payload": payload,
            "app_version": self._app_version,
            "client_env": self._client_env,
            "created_at": time.time(),
        })

    # -- session events --

    def session_start(self):
        self._session_start_ts = time.time()
        self._session_id = str(uuid.uuid4())
        self._fish_caught = 0
        self._fish_failed = 0
        self._emit("session", "session_start", {
            "session_id": self._session_id,
            "version": self._app_version,
            "env": self._client_env,
        })

    def session_end(self, reason: str = "normal"):
        duration = time.time() - self._session_start_ts if self._session_start_ts else 0
        self._emit("session", "session_end", {
            "session_id": self._session_id,
            "duration_s": round(duration, 1),
            "fish_caught": self._fish_caught,
            "fish_failed": self._fish_failed,
            "reason": reason,
        })

    # -- fishing events --

    def fishing_success(self, fish_name: str, weight: float, rarity: str,
                        round_duration: float, state_durations: Optional[dict] = None):
        self._fish_caught += 1
        self._emit("fishing", "fishing_success", {
            "fish_name": fish_name[:100],
            "weight": round(weight, 1),
            "rarity": rarity[:50],
            "round_s": round(round_duration, 2),
            "state_ms": state_durations or {},
        })

    def fishing_failed(self, reason: str, round_duration: float):
        self._fish_failed += 1
        self._emit("fishing", "fishing_failed", {
            "reason": reason[:100],
            "round_s": round(round_duration, 2),
        })

    # -- state machine events --

    def state_transition(self, from_state: int, to_state: int, duration_ms: float):
        self._emit("perf", "state_transition", {
            "from": from_state,
            "to": to_state,
            "duration_ms": round(duration_ms, 1),
        })

    # pid_metrics removed — high-frequency PID events (10Hz) overflowed the buffer.
    # Aggregate PID stats are reported via perf_snapshot instead.

    def detection_confidence(self, detector_type: str, confidence: float):
        self._emit("perf", "detection_confidence", {
            "type": detector_type[:32],
            "confidence": round(confidence, 4),
        })

    # -- vision events --

    def ocr_result(self, text: str, confidence: float, latency_ms: float, success: bool):
        self._emit("perf", "ocr_result", {
            "text": text[:100],
            "confidence": round(confidence, 4),
            "latency_ms": round(latency_ms, 1),
            "success": success,
        })

    def template_match(self, template_name: str, confidence: float, latency_ms: float, matched: bool):
        self._emit("perf", "template_match", {
            "template": template_name[:100],
            "confidence": round(confidence, 4),
            "latency_ms": round(latency_ms, 1),
            "matched": matched,
        })

    # -- UI behavior events (routed to Umami) --

    def ui_click(self, button_id: str, page: str = ""):
        self._emit("ui", "ui_button_click", {"button": button_id[:64], "page": page[:32]})

    def ui_page_view(self, page: str, duration_ms: float = 0):
        self._emit("ui", "ui_page_view", {"page": page[:64], "duration_ms": round(duration_ms)})

    def ui_feature_toggle(self, feature: str, enabled: bool):
        self._emit("ui", "ui_feature_use", {
            "feature": feature[:32],
            "action": "enable" if enabled else "disable",
        })

    def ui_float_window(self, action: str):
        self._emit("ui", "ui_float_window", {"action": action[:16]})

    def ui_share_export(self, fmt: str = "png"):
        self._emit("ui", "ui_share_export", {"format": fmt})

    # -- performance sampling --

    def perf_sample_fps(self, fps: float):
        self._perf.sample_fps(fps)

    def perf_sample_memory(self):
        self._perf.sample_memory()

    def perf_sample_latency(self, category: str, ms: float):
        self._perf.sample_latency(category, ms)

    def flush_perf(self):
        data = self._perf.get_and_reset()
        if data:
            self._emit("perf", "perf_snapshot", data)

    # -- record sync --

    def sync_records(self, records_data: dict):
        now = time.time()
        if now - self._last_sync_ts < 60:
            return
        self._last_sync_ts = now
        self._emit("record_sync", "records_sync", records_data)

    # -- error reporting --

    def error_report(self, error_type: str, message: str, stacktrace: str = "", context: Optional[dict] = None):
        self._emit("error", "error_report", {
            "error_id": uuid.uuid4().hex[:16],
            "error_type": error_type[:64],
            "error_message": message[:500],
            "stacktrace": stacktrace[:8192],
            "context": context or {},
            "occurred_at": time.time(),
        })
