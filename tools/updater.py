import argparse
import ctypes
from ctypes import wintypes
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path, PurePosixPath

try:
    import tkinter as tk
    from tkinter import ttk
except Exception:
    tk = None
    ttk = None


APP_NAME = "YHoAutoFish"
UPDATE_WORK_DIR_NAME = ".updates"
PROTECTED_NAMES = {
    "auth_device.json",
    "auth_device.json.tmp",
    "auth_state.dat",
    "auth_state.dat.tmp",
    "config.json",
    "records.json",
    "records.db",
    "records - 副本.json",
}
PROTECTED_DIRS = {
    "logs",
    "screenshots",
    "captures",
    UPDATE_WORK_DIR_NAME,
}
PROTECTED_PREFIXES = (
    ".records.",
    "debug_",
)

PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
WM_CLOSE = 0x0010


def log(app_dir, message):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}\n"
    try:
        logs_dir = Path(app_dir) / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        with open(logs_dir / "update.log", "a", encoding="utf-8") as file:
            file.write(line)
    except OSError:
        pass


def normalize_path(path):
    try:
        return os.path.normcase(str(Path(path).resolve()))
    except OSError:
        return os.path.normcase(str(Path(path).absolute()))


def _kernel32():
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _open_process(pid, access):
    if os.name != "nt":
        return None
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    kernel32 = _kernel32()
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    return open_process(access, False, pid)


def _close_handle(handle):
    if handle and os.name == "nt":
        kernel32 = _kernel32()
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(handle)


def query_process_path(pid):
    if os.name != "nt":
        return ""
    handle = _open_process(pid, PROCESS_QUERY_LIMITED_INFORMATION)
    if not handle:
        return ""
    try:
        kernel32 = _kernel32()
        query = kernel32.QueryFullProcessImageNameW
        query.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        query.restype = wintypes.BOOL
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if query(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
    finally:
        _close_handle(handle)
    return ""


def is_process_running(pid):
    if os.name != "nt":
        return False
    handle = _open_process(pid, SYNCHRONIZE)
    if not handle:
        return False
    try:
        kernel32 = _kernel32()
        wait = kernel32.WaitForSingleObject
        wait.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        wait.restype = wintypes.DWORD
        return wait(handle, 0) == WAIT_TIMEOUT
    finally:
        _close_handle(handle)


def wait_for_pid_exit(pid, timeout=10):
    deadline = time.time() + max(0.1, float(timeout))
    while time.time() < deadline:
        if not is_process_running(pid):
            return True
        time.sleep(0.12)
    return not is_process_running(pid)


def enumerate_process_ids():
    if os.name != "nt":
        return []
    try:
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        enum_processes = psapi.EnumProcesses
        enum_processes.argtypes = [
            ctypes.POINTER(wintypes.DWORD),
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        enum_processes.restype = wintypes.BOOL
        capacity = 4096
        while True:
            processes = (wintypes.DWORD * capacity)()
            bytes_returned = wintypes.DWORD()
            if not enum_processes(processes, ctypes.sizeof(processes), ctypes.byref(bytes_returned)):
                return []
            count = bytes_returned.value // ctypes.sizeof(wintypes.DWORD)
            if count < capacity:
                return [int(processes[index]) for index in range(count) if int(processes[index]) > 0]
            capacity *= 2
    except Exception:
        return []


def process_matches_path(pid, expected_path):
    process_path = query_process_path(pid)
    return bool(process_path) and normalize_path(process_path) == normalize_path(expected_path)


def collect_app_processes(app_exe, primary_pid=None):
    if os.name != "nt":
        return []
    current_pid = os.getpid()
    targets = set()
    for pid in enumerate_process_ids():
        if pid == current_pid:
            continue
        if process_matches_path(pid, app_exe):
            targets.add(pid)
    try:
        primary_pid = int(primary_pid or 0)
    except (TypeError, ValueError):
        primary_pid = 0
    if primary_pid > 0 and primary_pid != current_pid and process_matches_path(primary_pid, app_exe):
        targets.add(primary_pid)
    return sorted(targets)


def post_close_to_processes(pids):
    if os.name != "nt" or not pids:
        return 0
    target_pids = {int(pid) for pid in pids}
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    enum_windows = user32.EnumWindows
    enum_windows.argtypes = [ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM), wintypes.LPARAM]
    enum_windows.restype = wintypes.BOOL
    get_window_thread_process_id = user32.GetWindowThreadProcessId
    get_window_thread_process_id.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    get_window_thread_process_id.restype = wintypes.DWORD
    is_window_visible = user32.IsWindowVisible
    is_window_visible.argtypes = [wintypes.HWND]
    is_window_visible.restype = wintypes.BOOL
    post_message = user32.PostMessageW
    post_message.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    post_message.restype = wintypes.BOOL
    posted = 0

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):
        nonlocal posted
        pid = wintypes.DWORD()
        get_window_thread_process_id(hwnd, ctypes.byref(pid))
        if pid.value in target_pids and is_window_visible(hwnd):
            if post_message(hwnd, WM_CLOSE, 0, 0):
                posted += 1
        return True

    enum_windows(callback, 0)
    return posted


def terminate_process(pid, expected_exe):
    if os.name != "nt":
        return False
    if pid == os.getpid() or not process_matches_path(pid, expected_exe):
        return False
    handle = _open_process(pid, PROCESS_TERMINATE | SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION)
    if not handle:
        return False
    try:
        kernel32 = _kernel32()
        terminate = kernel32.TerminateProcess
        terminate.argtypes = [wintypes.HANDLE, wintypes.UINT]
        terminate.restype = wintypes.BOOL
        return bool(terminate(handle, 0))
    finally:
        _close_handle(handle)


def close_running_app(app_dir, exe_name, primary_pid, reporter, wait_timeout):
    app_exe = (app_dir / exe_name).resolve()
    if os.name != "nt":
        reporter.detail("非 Windows 环境，等待旧进程释放文件。")
        time.sleep(1.5)
        return

    pids = collect_app_processes(app_exe, primary_pid=primary_pid)
    if not pids:
        reporter.detail("未发现仍在运行的旧版本进程。")
        return

    reporter.detail(f"正在关闭旧版本进程：{', '.join(str(pid) for pid in pids)}")
    posted = post_close_to_processes(pids)
    if posted:
        reporter.detail(f"已向旧版本窗口发送关闭请求，等待进程退出。")
    graceful_timeout = min(max(3, int(wait_timeout * 0.4)), 15)
    deadline = time.time() + graceful_timeout
    while time.time() < deadline:
        remaining = [pid for pid in pids if is_process_running(pid)]
        if not remaining:
            reporter.detail("旧版本已正常退出。")
            return
        time.sleep(0.15)

    remaining = [pid for pid in pids if is_process_running(pid)]
    if not remaining:
        reporter.detail("旧版本已正常退出。")
        return

    reporter.detail("旧版本未及时退出，正在安全终止目标程序进程。")
    for pid in remaining:
        terminate_process(pid, app_exe)
    force_deadline = time.time() + min(max(3, int(wait_timeout * 0.25)), 10)
    while time.time() < force_deadline:
        remaining = [pid for pid in remaining if is_process_running(pid)]
        if not remaining:
            reporter.detail("旧版本进程已关闭，继续安装。")
            return
        time.sleep(0.15)
    if remaining:
        raise TimeoutError(f"旧版本进程仍未退出：{', '.join(str(pid) for pid in remaining)}")


def safe_member_path(extract_root, member_name):
    member_path = PurePosixPath(member_name)
    if member_path.is_absolute() or ".." in member_path.parts:
        raise RuntimeError(f"更新包包含非法路径: {member_name}")
    target = (extract_root / Path(*member_path.parts)).resolve()
    root = extract_root.resolve()
    if target != root and root not in target.parents:
        raise RuntimeError(f"更新包路径越界: {member_name}")
    return target


def extract_zip_safely(zip_path, extract_root, progress_callback=None):
    extract_root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        entries = archive.infolist()
        total = max(1, len(entries))
        for index, info in enumerate(entries, start=1):
            target = safe_member_path(extract_root, info.filename)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, open(target, "wb") as destination:
                    shutil.copyfileobj(source, destination)
            if progress_callback:
                progress_callback(index, total, info.filename)


def find_payload_root(extract_root, exe_name):
    direct = extract_root / exe_name
    if direct.exists():
        return extract_root
    children = [path for path in extract_root.iterdir() if path.is_dir()]
    for child in children:
        if (child / exe_name).exists():
            return child
    if len(children) == 1:
        return children[0]
    raise RuntimeError(f"更新包内未找到 {exe_name}")


def is_protected(relative_path):
    parts = relative_path.parts
    if not parts:
        return True
    first = parts[0].lower()
    name = relative_path.name.lower()
    if first in PROTECTED_DIRS:
        return True
    if name in PROTECTED_NAMES:
        return True
    return any(name.startswith(prefix) for prefix in PROTECTED_PREFIXES)


def copy_with_retries(source, target, attempts=10):
    target.parent.mkdir(parents=True, exist_ok=True)
    last_error = None
    for attempt in range(attempts):
        try:
            shutil.copy2(source, target)
            return
        except PermissionError as exc:
            last_error = exc
            time.sleep(0.45 + attempt * 0.08)
        except OSError as exc:
            last_error = exc
            time.sleep(0.18 + attempt * 0.04)
    raise last_error or RuntimeError(f"复制失败: {source} -> {target}")


def current_process_path():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    return Path(__file__).resolve()


def apply_payload(payload_root, app_dir, runner_path=None, progress_callback=None):
    copied = 0
    skipped = 0
    files = [source for source in payload_root.rglob("*") if source.is_file()]
    total = max(1, len(files))
    runner_path = Path(runner_path).resolve() if runner_path else None
    for index, source in enumerate(files, start=1):
        relative = source.relative_to(payload_root)
        if is_protected(relative):
            skipped += 1
        else:
            target = app_dir / relative
            if runner_path is not None:
                try:
                    if target.resolve() == runner_path:
                        skipped += 1
                        if progress_callback:
                            progress_callback(index, total, relative, copied, skipped)
                        continue
                except OSError:
                    pass
            copy_with_retries(source, target)
            copied += 1
        if progress_callback:
            progress_callback(index, total, relative, copied, skipped)
    return copied, skipped


def restart_app(app_dir, exe_name):
    exe_path = app_dir / exe_name
    if exe_path.exists():
        subprocess.Popen([str(exe_path)], cwd=str(app_dir), close_fds=True)


def update_work_dir(app_dir):
    root = Path(app_dir).resolve() / UPDATE_WORK_DIR_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def update_subdir(app_dir, name):
    root = update_work_dir(app_dir) / name
    root.mkdir(parents=True, exist_ok=True)
    return root


def cleanup_old_update_children(root, max_age_seconds=86400, keep_paths=None):
    root = Path(root)
    if not root.exists():
        return
    keep = set()
    for path in keep_paths or ():
        try:
            keep.add(Path(path).resolve())
        except OSError:
            pass
    now = time.time()
    for child in root.iterdir():
        try:
            resolved = child.resolve()
            if resolved in keep:
                continue
            if now - child.stat().st_mtime < max_age_seconds:
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink()
        except OSError:
            continue


def cleanup_update_workspace(app_dir, current_runner=None, current_extract_root=None):
    work_dir = update_work_dir(app_dir)
    cleanup_old_update_children(work_dir / "downloads", max_age_seconds=86400)
    cleanup_old_update_children(work_dir / "apply", max_age_seconds=86400, keep_paths=[current_extract_root] if current_extract_root else None)
    cleanup_old_update_children(work_dir / "runners", max_age_seconds=86400, keep_paths=[current_runner] if current_runner else None)


def remove_if_update_download(path, app_dir):
    try:
        download_root = update_subdir(app_dir, "downloads").resolve()
        resolved = Path(path).resolve()
        if resolved != download_root and download_root in resolved.parents and resolved.exists():
            resolved.unlink()
    except OSError:
        pass


class HeadlessReporter:
    def __init__(self, app_dir):
        self.app_dir = app_dir

    def phase(self, title, percent=None, detail=None):
        message = title if detail is None else f"{title}: {detail}"
        log(self.app_dir, message)

    def progress(self, percent, detail=None):
        if detail:
            log(self.app_dir, f"安装进度 {percent}%: {detail}")

    def progress_update(self, percent, detail=None):
        self.progress(percent, detail)

    def detail(self, message):
        log(self.app_dir, message)

    def complete(self, message, app_dir, exe_name):
        log(app_dir, message)
        restart_app(app_dir, exe_name)

    def fail(self, message):
        log(self.app_dir, f"更新失败: {message}")


class InstallerWindow:
    def __init__(self, args):
        self.args = args
        self.events = queue.Queue()
        self.exit_code = 1
        self.finished = False
        self.success = False
        self.app_dir = Path(args.app_dir).resolve()
        self.root = tk.Tk()
        self.root.title("YHo AutoFish 更新安装器")
        self.root.geometry("660x430")
        self.root.minsize(620, 390)
        self.root.configure(bg="#0B1624")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._handle_close)
        try:
            self.root.attributes("-topmost", True)
            self.root.after(1400, lambda: self.root.attributes("-topmost", False))
        except Exception:
            pass
        self._center()
        self._build_ui()

    def _center(self):
        self.root.update_idletasks()
        width = self.root.winfo_width()
        height = self.root.winfo_height()
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        x = max(0, (screen_width - width) // 2)
        y = max(0, (screen_height - height) // 2)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _build_ui(self):
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(0, weight=1)

        shell = tk.Frame(self.root, bg="#0F2034", highlightthickness=1, highlightbackground="#2DD4D7")
        shell.grid(row=0, column=0, sticky="nsew", padx=18, pady=18)
        shell.grid_columnconfigure(0, weight=1)

        target_version = f" v{self.args.version}" if self.args.version else ""
        title = tk.Label(
            shell,
            text=f"正在安装 YHo AutoFish{target_version}",
            bg="#0F2034",
            fg="#F3F8FF",
            font=("Microsoft YaHei UI", 22, "bold"),
            anchor="w",
        )
        title.grid(row=0, column=0, sticky="ew", padx=26, pady=(24, 8))

        self.subtitle = tk.Label(
            shell,
            text="安装器会先关闭旧版本，再覆盖程序文件。用户配置、捕获记录和日志不会被覆盖。",
            bg="#0F2034",
            fg="#63E4E4",
            font=("Microsoft YaHei UI", 10, "bold"),
            anchor="w",
            wraplength=590,
            justify="left",
        )
        self.subtitle.grid(row=1, column=0, sticky="ew", padx=26, pady=(0, 18))

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "YHo.Horizontal.TProgressbar",
            troughcolor="#1A2A3D",
            bordercolor="#1A2A3D",
            background="#22D3D6",
            lightcolor="#22D3D6",
            darkcolor="#22D3D6",
        )
        self.progress = ttk.Progressbar(
            shell,
            style="YHo.Horizontal.TProgressbar",
            mode="determinate",
            maximum=100,
            value=0,
        )
        self.progress.grid(row=2, column=0, sticky="ew", padx=26, pady=(0, 10))

        self.percent_label = tk.Label(
            shell,
            text="0%",
            bg="#0F2034",
            fg="#DDE7F5",
            font=("Microsoft YaHei UI", 10, "bold"),
            anchor="e",
        )
        self.percent_label.grid(row=3, column=0, sticky="ew", padx=26)

        self.status = tk.Label(
            shell,
            text="准备安装...",
            bg="#0F2034",
            fg="#F3F8FF",
            font=("Microsoft YaHei UI", 13, "bold"),
            anchor="w",
            wraplength=590,
            justify="left",
        )
        self.status.grid(row=4, column=0, sticky="ew", padx=26, pady=(16, 6))

        self.detail_label = tk.Label(
            shell,
            text="请等待安装器完成操作。",
            bg="#0F2034",
            fg="#9AB0CA",
            font=("Microsoft YaHei UI", 10),
            anchor="nw",
            wraplength=590,
            justify="left",
        )
        self.detail_label.grid(row=5, column=0, sticky="nsew", padx=26, pady=(0, 18))
        shell.grid_rowconfigure(5, weight=1)

        self.action_row = tk.Frame(shell, bg="#0F2034")
        self.action_row.grid(row=6, column=0, sticky="ew", padx=26, pady=(0, 24))
        self.action_row.grid_columnconfigure(0, weight=1)

        self.launch_button = tk.Button(
            self.action_row,
            text="启动新版",
            state="disabled",
            command=self._launch_and_exit,
            bg="#22D3D6",
            fg="#06242A",
            activebackground="#63E4E4",
            activeforeground="#06242A",
            relief="flat",
            padx=22,
            pady=9,
            font=("Microsoft YaHei UI", 10, "bold"),
            cursor="hand2",
        )
        self.launch_button.grid(row=0, column=1, padx=(10, 0))

        self.finish_button = tk.Button(
            self.action_row,
            text="安装中，请稍候",
            state="disabled",
            command=self._finish,
            bg="#1A2A3D",
            fg="#9AB0CA",
            activebackground="#25384F",
            activeforeground="#F3F8FF",
            relief="flat",
            padx=22,
            pady=9,
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        self.finish_button.grid(row=0, column=2, padx=(10, 0))

    def _handle_close(self):
        if self.finished:
            self._finish()
            return
        self.detail("安装进行中，请等待完成后再关闭窗口。")

    def _set_percent(self, percent):
        percent = max(0, min(100, int(percent)))
        self.progress["value"] = percent
        self.percent_label.configure(text=f"{percent}%")

    def phase(self, title, percent=None, detail=None):
        self.events.put(("phase", title, percent, detail))

    def progress_update(self, percent, detail=None):
        self.events.put(("progress", percent, detail))

    def detail(self, message):
        self.events.put(("detail", message))

    def complete(self, message, app_dir, exe_name):
        self.events.put(("complete", message, str(app_dir), exe_name))

    def fail(self, message):
        self.events.put(("fail", message))

    def _poll_events(self):
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            kind = event[0]
            if kind == "phase":
                _, title, percent, detail = event
                self.status.configure(text=title, fg="#F3F8FF")
                if percent is not None:
                    self._set_percent(percent)
                if detail:
                    self.detail_label.configure(text=detail, fg="#9AB0CA")
            elif kind == "progress":
                _, percent, detail = event
                self._set_percent(percent)
                if detail:
                    self.detail_label.configure(text=detail, fg="#9AB0CA")
            elif kind == "detail":
                self.detail_label.configure(text=event[1], fg="#9AB0CA")
            elif kind == "complete":
                _, message, app_dir, exe_name = event
                self.finished = True
                self.success = True
                self.exit_code = 0
                self.app_dir = Path(app_dir)
                self.exe_name = exe_name
                self._set_percent(100)
                self.status.configure(text="安装完成", fg="#63E4E4")
                self.detail_label.configure(text=message, fg="#DDE7F5")
                self.launch_button.configure(state="normal")
                self.finish_button.configure(text="完成退出", state="normal", fg="#DDE7F5", cursor="hand2")
            elif kind == "fail":
                self.finished = True
                self.success = False
                self.exit_code = 1
                self.status.configure(text="安装失败", fg="#FF647C")
                self.detail_label.configure(text=event[1], fg="#FFB4C0")
                self.finish_button.configure(text="退出安装器", state="normal", fg="#DDE7F5", cursor="hand2")
        self.root.after(80, self._poll_events)

    def _worker(self):
        try:
            perform_update(self.args, self)
        except Exception as exc:
            self.fail(str(exc))

    def _launch_and_exit(self):
        try:
            restart_app(self.app_dir, self.exe_name)
        except Exception as exc:
            self.detail_label.configure(text=f"启动新版失败：{exc}", fg="#FFB4C0")
            return
        self.exit_code = 0
        self.root.destroy()

    def _finish(self):
        self.root.destroy()

    def run(self):
        threading.Thread(target=self._worker, daemon=True).start()
        self.root.after(80, self._poll_events)
        self.root.mainloop()
        return self.exit_code


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="YHo AutoFish external updater")
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--package", required=True)
    parser.add_argument("--app-dir", required=True)
    parser.add_argument("--exe", default=f"{APP_NAME}.exe")
    parser.add_argument("--version", default="")
    parser.add_argument("--wait-timeout", type=int, default=90)
    parser.add_argument("--silent", action="store_true")
    return parser.parse_args(argv)


def perform_update(args, reporter):
    app_dir = Path(args.app_dir).resolve()
    package = Path(args.package).resolve()
    runner_path = current_process_path()
    extract_root = update_subdir(app_dir, "apply") / str(os.getpid())
    if int(args.pid) == os.getpid():
        raise RuntimeError("主程序 PID 不能是更新器自身 PID，已拒绝继续更新")
    if not package.exists():
        raise RuntimeError("更新包不存在，无法安装")

    try:
        log(app_dir, "更新器已启动，准备安装")
        cleanup_update_workspace(app_dir, current_runner=runner_path.parent, current_extract_root=extract_root)
        reporter.phase("正在关闭旧版本", 8, "为避免文件被占用，安装器会先关闭旧版本程序。")
        close_running_app(app_dir, args.exe, args.pid, reporter, args.wait_timeout)

        reporter.phase("正在解压更新包", 22, "正在解压新版文件，请稍候。")

        def on_extract(index, total, filename):
            percent = 22 + int(index * 16 / max(1, total))
            if index == total or index % 25 == 0:
                reporter.progress_update(percent, f"正在解压：{filename}")

        extract_zip_safely(package, extract_root, progress_callback=on_extract)

        reporter.phase("正在检查发布包", 39, "正在定位新版主程序。")
        payload_root = find_payload_root(extract_root, args.exe)

        reporter.phase("正在安装新版文件", 42, "正在覆盖程序文件，用户配置和捕获记录会被保留。")

        def on_copy(index, total, relative, copied, skipped):
            percent = 42 + int(index * 50 / max(1, total))
            if index == total or index % 10 == 0:
                reporter.progress_update(percent, f"已处理 {index}/{total} 个文件，复制 {copied} 个，保留用户数据 {skipped} 个。")

        copied, skipped = apply_payload(payload_root, app_dir, runner_path=runner_path, progress_callback=on_copy)
        log(app_dir, f"文件覆盖完成，复制 {copied} 个文件，跳过用户数据 {skipped} 个文件")

        reporter.phase("正在清理临时文件", 96, "正在清理下载包和临时解压目录。")
        remove_if_update_download(package, app_dir)
        shutil.rmtree(extract_root, ignore_errors=True)
        log(app_dir, "更新安装完成，等待用户选择是否启动新版")
        reporter.complete("新版已经安装完成。你可以立即启动新版，也可以先退出安装器稍后手动启动。", app_dir, args.exe)
    except Exception:
        shutil.rmtree(extract_root, ignore_errors=True)
        raise


def main(argv=None):
    args = parse_args(argv)
    app_dir = Path(args.app_dir).resolve()
    if not args.silent and tk is not None and ttk is not None:
        window = InstallerWindow(args)
        return window.run()

    reporter = HeadlessReporter(app_dir)
    try:
        perform_update(args, reporter)
        return 0
    except Exception as exc:
        reporter.fail(str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
