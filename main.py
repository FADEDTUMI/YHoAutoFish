import sys
import os
import ssl

# 版本标记：用于确认打包后是否使用了最新的代码
_SSL_PATCH_VERSION = "2.0.0"

# 确保 frozen 模式下 SSL 证书可用（monkey-patch ssl 模块）
_cacert_path = None

# 构建搜索路径列表（按优先级排序）
_search_bases = []

# 路径1: _MEIPASS（仅 onefile 模式存在）
_meipass = getattr(sys, '_MEIPASS', None)
if _meipass:
    _search_bases.append(_meipass)

# 路径2: exe 所在目录的 _internal 子目录（onedir 模式 PyInstaller 资源目录）
_exe_dir = os.path.dirname(sys.executable)
_internal_dir = os.path.join(_exe_dir, '_internal')
if os.path.isdir(_internal_dir):
    _search_bases.append(_internal_dir)

# 路径3: exe 所在目录（onedir 模式的根目录）
_search_bases.append(_exe_dir)

# 路径4: 脚本文件所在目录（开发环境）
_script_dir = os.path.dirname(os.path.abspath(__file__))
if _script_dir not in _search_bases:
    _search_bases.append(_script_dir)

# 搜索 certifi/cacert.pem（本地文件优先，避免依赖 certifi.where() 的绝对路径）
for _base in _search_bases:
    _candidate = os.path.join(_base, 'certifi', 'cacert.pem')
    if os.path.isfile(_candidate):
        _cacert_path = _candidate
        break

# 策略5: certifi 包（开发环境回退，frozen 模式下可能返回构建机器路径，需验证文件存在）
if not _cacert_path:
    try:
        import certifi
        _p = certifi.where()
        if _p and os.path.isfile(_p):
            _cacert_path = _p
    except Exception:
        pass

# 策略6: 搜索 certs/ 目录下的 cacert.pem（自定义 CA 也可用作回退）
if not _cacert_path:
    for _base in _search_bases:
        _candidate = os.path.join(_base, 'certs', 'cacert.pem')
        if os.path.isfile(_candidate):
            _cacert_path = _candidate
            break

# 写诊断日志到 stderr（frozen 模式下可见）
_ssl_diag = f"[SSL] version={_SSL_PATCH_VERSION}, cacert_path={_cacert_path}, frozen={getattr(sys, 'frozen', False)}, _MEIPASS={getattr(sys, '_MEIPASS', 'N/A')}, exe_dir={_exe_dir}"
print(_ssl_diag, file=sys.stderr)

if _cacert_path and os.path.isfile(_cacert_path):
    os.environ['SSL_CERT_FILE'] = _cacert_path
    _orig_create_default_ctx = ssl.create_default_context
    def _patched_create_default_context(*args, **kwargs):
        ctx = _orig_create_default_ctx(*args, **kwargs)
        if 'cafile' not in kwargs and 'capath' not in kwargs:
            try:
                ctx.load_verify_locations(cafile=_cacert_path)
            except Exception:
                pass
        return ctx
    ssl.create_default_context = _patched_create_default_context
    print(f"[SSL] monkey-patch OK, CA file: {_cacert_path}", file=sys.stderr)
else:
    print(f"[SSL] WARNING: no CA file found, SSL may fail!", file=sys.stderr)
    print(f"[SSL] Search bases: {_search_bases}", file=sys.stderr)

# Ensure modules can be found
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from core.admin import ensure_admin_or_relaunch

if not ensure_admin_or_relaunch():
    sys.exit(0)

from core.dpi import set_process_dpi_awareness

set_process_dpi_awareness()

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QIcon
from core.paths import resource_path
from core.version import APP_DISPLAY_NAME, APP_NAME, APP_VERSION
from gui.app import AppWindow
from core.tracker import EventTracker
from core.error_handler import GlobalErrorHandler

if __name__ == '__main__':
    print("Starting app...", flush=True)
    app = QApplication(sys.argv)
    
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_DISPLAY_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setWindowIcon(QIcon(resource_path("logo.jpg")))
    
    print("Creating AppWindow...", flush=True)
    window = AppWindow()
    print("Showing AppWindow...", flush=True)
    window.show()

    # Initialize analytics tracker
    tracker = EventTracker.get()
    GlobalErrorHandler.install(tracker)

    exit_code = app.exec()
    EventTracker.get().shutdown()
    sys.exit(exit_code)
