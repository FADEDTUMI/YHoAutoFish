import shutil
import sys
from pathlib import Path


def _is_frozen_app():
    return bool(getattr(sys, "frozen", False) or "__compiled__" in globals())


def app_base_dir():
    if _is_frozen_app():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def resource_path(*parts):
    """获取资源文件路径，优先使用 _MEIPASS（onefile 模式），其次 _internal 子目录（onedir 模式）。"""
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir:
        # onefile 模式：资源在 _MEIPASS 临时目录
        return str(Path(bundle_dir).resolve().joinpath(*parts))

    if _is_frozen_app():
        # onedir 模式：资源在 exe 同级的 _internal 目录
        exe_dir = Path(sys.executable).resolve().parent
        internal_candidate = exe_dir.joinpath("_internal", *parts)
        if internal_candidate.exists():
            return str(internal_candidate)
        # 回退：exe 同级目录（用户手动放置的资源）
        return str(exe_dir.joinpath(*parts))

    # 开发环境：脚本所在目录
    return str(Path(__file__).resolve().parents[1].joinpath(*parts))


def writable_path(*parts):
    return str(app_base_dir().joinpath(*parts))


def ensure_writable_file(filename):
    target = Path(writable_path(filename))
    source = Path(resource_path(filename))
    if not target.exists() and source.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() != target.resolve():
            shutil.copy2(source, target)
    return str(target)
