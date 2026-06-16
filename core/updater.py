import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path

from core.paths import app_base_dir
from core.version import APP_NAME, APP_REPOSITORY_URL, APP_VERSION

try:
    from core.version import APP_GITEE_REPOSITORY_URL
except ImportError:
    APP_GITEE_REPOSITORY_URL = ""


LATEST_RELEASE_API = "https://api.github.com/repos/FADEDTUMI/YHoAutoFish/releases/latest"
LATEST_MANIFEST_URL = f"{APP_REPOSITORY_URL}/releases/latest/download/latest.json"
ENABLE_GITHUB_API_FALLBACK = False
USER_AGENT = f"{APP_NAME}/{APP_VERSION}"
DEFAULT_MIRROR_PREFIXES = (
    "https://gh.llkk.cc/",
    "https://ghproxy.net/",
    "https://mirror.ghproxy.com/",
)
MIRROR_PREFIXES = DEFAULT_MIRROR_PREFIXES
UPDATE_CONFIG_NAME = "config.json"
UPDATE_SOURCE_GITHUB = "github"
UPDATE_SOURCE_GITEE = "gitee"
UPDATE_SOURCE_AUTO = "auto"
UPDATE_WORK_DIR_NAME = ".updates"
UPDATE_DOWNLOAD_DIR_NAME = "downloads"
UPDATE_RUNNER_DIR_NAME = "runners"


class UpdateError(RuntimeError):
    pass


class DownloadCancelled(UpdateError):
    pass


class NoPublishedRelease(UpdateError):
    pass


class ManifestUnavailable(UpdateError):
    pass


@dataclass
class UpdateInfo:
    version: str
    tag_name: str
    release_name: str
    body: str
    asset_name: str
    download_url: str
    html_url: str
    github_html_url: str = ""
    gitee_html_url: str = ""
    digest: str = ""
    github_digest: str = ""
    gitee_digest: str = ""
    download_urls: tuple = ()
    github_download_urls: tuple = ()
    gitee_download_urls: tuple = ()
    asset_parts: tuple = ()
    gitee_asset_parts: tuple = ()
    gitee_release_tag: str = ""
    gitee_release_asset_names: tuple = ()
    source: str = UPDATE_SOURCE_GITHUB

    @property
    def sha256(self):
        return _normalize_sha256(self.digest)


def _normalize_sha256(value):
    prefix = "sha256:"
    value = (value or "").strip()
    if value.lower().startswith(prefix):
        value = value[len(prefix):].strip()
    if re.fullmatch(r"[a-fA-F0-9]{64}", value):
        return value.lower()
    return ""


def _expected_sha256_for_source(update_info, source):
    source = str(source or UPDATE_SOURCE_GITHUB).strip().lower()
    if source == UPDATE_SOURCE_GITEE:
        return _normalize_sha256(getattr(update_info, "gitee_digest", "")) or update_info.sha256
    if source == UPDATE_SOURCE_GITHUB:
        return _normalize_sha256(getattr(update_info, "github_digest", "")) or update_info.sha256
    return update_info.sha256


def _raise_if_cancelled(cancel_callback):
    if cancel_callback is None:
        return
    try:
        cancelled = bool(cancel_callback())
    except Exception:
        cancelled = False
    if cancelled:
        raise DownloadCancelled("更新下载已取消。")


def parse_version(version_text):
    parts = re.findall(r"\d+", str(version_text or ""))
    normalized = [int(part) for part in parts[:4]]
    while len(normalized) < 4:
        normalized.append(0)
    return tuple(normalized)


def is_newer_version(remote_version, current_version=APP_VERSION):
    return parse_version(remote_version) > parse_version(current_version)

def _ssl_context_with_certifi():
    """创建 SSL context，frozen 模式下 main.py 的 monkey-patch 会自动叠加 certifi CA。"""
    import ssl as _ssl
    return _ssl.create_default_context()


def _request(url, timeout=8, api=False):
    headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": USER_AGENT,
    }
    if api:
        headers.update(
            {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )
    request = urllib.request.Request(
        url,
        headers=headers,
    )
    return urllib.request.urlopen(request, timeout=timeout, context=_ssl_context_with_certifi())


def _load_json(url, timeout=8, label="GitHub Release 信息", api=False):
    service_name = "Gitee" if _is_gitee_url(url) else "GitHub"
    try:
        with _request(url, timeout=timeout, api=api) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            text = response.read().decode(charset, errors="replace").lstrip("\ufeff")
            return json.loads(text)
    except urllib.error.HTTPError as exc:
        message = _format_http_error(exc, service_name=service_name)
        if exc.code == 404:
            if label == "更新清单":
                raise ManifestUnavailable(message) from exc
            raise NoPublishedRelease(message) from exc
        raise UpdateError(message) from exc
    except urllib.error.URLError as exc:
        raise UpdateError(f"无法连接 {service_name}: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        if label == "更新清单" and _looks_like_html(text):
            raise ManifestUnavailable("更新清单尚未发布，或当前网络返回了网页而不是 JSON") from exc
        raise UpdateError(f"{label}不是有效 JSON") from exc


def _looks_like_html(text):
    sample = str(text or "").lstrip()[:256].lower()
    return sample.startswith("<!doctype html") or sample.startswith("<html") or "<html" in sample


def _format_http_error(exc, service_name="GitHub"):
    detail = ""
    try:
        charset = exc.headers.get_content_charset() or "utf-8"
        raw_body = exc.read().decode(charset, errors="replace")
        payload = json.loads(raw_body) if raw_body else {}
        detail = str(payload.get("message") or "").strip()
    except Exception:
        detail = ""

    if exc.code == 404:
        return "当前仓库还没有可用于自动更新的正式 GitHub Release"

    if exc.code == 403:
        remaining = (exc.headers.get("X-RateLimit-Remaining") or "").strip()
        reset_at = _format_rate_limit_reset(exc.headers.get("X-RateLimit-Reset"))
        retry_after = (exc.headers.get("Retry-After") or "").strip()
        if remaining == "0":
            suffix = f"，预计 {reset_at} 后恢复" if reset_at else ""
            return f"{service_name} API 请求已达到未登录限额{suffix}"
        if retry_after:
            return f"{service_name} API 暂时拒绝频繁请求，建议 {retry_after} 秒后再试"
        if detail:
            return f"{service_name} API 访问被拒绝: {detail}"
        return f"{service_name} API 访问被拒绝，可能是网络代理、限流或仓库权限导致"

    if detail:
        return f"{service_name} 返回 HTTP {exc.code}: {detail}"
    return f"{service_name} 返回 HTTP {exc.code}"


def _format_rate_limit_reset(value):
    try:
        reset_ts = int(value)
    except (TypeError, ValueError):
        return ""
    if reset_ts <= 0:
        return ""
    return time.strftime("%H:%M:%S", time.localtime(reset_ts))


def _version_from_tag(tag_name):
    return str(tag_name or "").strip().lstrip("vV")


def _expected_asset_name(version):
    return f"{APP_NAME}-v{version}-windows.zip"


def _latest_asset_download_url(asset_name):
    return f"{APP_REPOSITORY_URL}/releases/latest/download/{asset_name}"


def _is_repo_release_download_url(url):
    return str(url or "").startswith(f"{APP_REPOSITORY_URL}/releases/download/")


def _is_github_url(url):
    normalized = str(url or "").lower()
    return normalized.startswith("https://github.com/") or normalized.startswith("https://api.github.com/")


def _is_gitee_url(url):
    normalized = str(url or "").lower()
    return normalized.startswith("https://gitee.com/") or normalized.startswith("https://api.gitee.com/")


def _is_gitee_api_download_url(url):
    normalized = str(url or "").lower()
    return (
        normalized.startswith("https://gitee.com/api/v5/")
        and "/attach_files/" in normalized
        and normalized.endswith("/download")
    )


def _coerce_url_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = re.split(r"[\r\n,;]+", value)
    elif isinstance(value, (list, tuple, set)):
        raw_items = value
    else:
        return []
    urls = []
    for item in raw_items:
        text = str(item or "").strip()
        if text and text not in urls:
            urls.append(text)
    return urls


def _read_update_config():
    config_path = Path(app_base_dir()) / UPDATE_CONFIG_NAME
    try:
        with open(config_path, "r", encoding="utf-8") as file:
            data = json.load(file)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def update_work_dir(app_dir=None):
    root = Path(app_dir or app_base_dir()).resolve() / UPDATE_WORK_DIR_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def _update_subdir(name, app_dir=None):
    root = update_work_dir(app_dir) / name
    root.mkdir(parents=True, exist_ok=True)
    return root


def _cleanup_old_children(root, max_age_seconds=86400):
    root = Path(root)
    if not root.exists():
        return
    now = time.time()
    for child in root.iterdir():
        try:
            if now - child.stat().st_mtime < max_age_seconds:
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink()
        except OSError:
            continue


def _gitee_repository_url():
    config = _read_update_config()
    configured = str(
        config.get("gitee_repository_url")
        or config.get("update_gitee_repository_url")
        or os.environ.get("YHO_GITEE_REPOSITORY_URL")
        or APP_GITEE_REPOSITORY_URL
        or ""
    ).strip()
    return configured.rstrip("/")


def _gitee_owner_repo():
    repo_url = _gitee_repository_url()
    match = re.match(r"https?://gitee\.com/([^/]+)/([^/#?]+)", repo_url, re.IGNORECASE)
    if not match:
        return "", ""
    owner, repo = match.group(1), match.group(2)
    if repo.endswith(".git"):
        repo = repo[:-4]
    return owner, repo


def _env_url_list(name):
    return _coerce_url_list(os.environ.get(name))


def _config_url_list(config, *keys):
    urls = []
    for key in keys:
        for url in _coerce_url_list(config.get(key)):
            if url not in urls:
                urls.append(url)
    return urls


def _merge_urls(*groups):
    urls = []
    for group in groups:
        for url in _coerce_url_list(group):
            if url not in urls:
                urls.append(url)
    return urls


def _mirror_prefixes(manifest=None):
    config = _read_update_config()
    manifest_prefixes = []
    if isinstance(manifest, dict):
        manifest_prefixes = _coerce_url_list(manifest.get("mirror_prefixes") or manifest.get("github_mirror_prefixes"))
    return tuple(
        _merge_urls(
            _config_url_list(config, "update_mirror_prefixes", "github_mirror_prefixes"),
            _env_url_list("YHO_UPDATE_MIRROR_PREFIXES"),
            manifest_prefixes,
            DEFAULT_MIRROR_PREFIXES,
        )
    )


def _format_url_template(url, version="", tag_name="", asset_name=""):
    text = str(url or "").strip()
    if not text:
        return ""
    try:
        return text.format(version=version, tag=tag_name, tag_name=tag_name, asset=asset_name, asset_name=asset_name)
    except Exception:
        return text


def _asset_name_from_release_asset(asset):
    if not isinstance(asset, dict):
        return ""
    return str(asset.get("name") or asset.get("filename") or asset.get("file_name") or "").strip()


def _release_asset_names(release):
    names = []
    for asset in _extract_release_assets(release):
        name = _asset_name_from_release_asset(asset)
        if name and name not in names:
            names.append(name)
    return names


def _split_asset_groups(release):
    groups = {}
    for name in _release_asset_names(release):
        text = str(name or "").strip()
        if not text:
            continue
        matched = False
        split_match = re.fullmatch(r"(.+)\.(\d{2,4})", text)
        if split_match:
            base = split_match.group(1)
            index = int(split_match.group(2))
            groups.setdefault(base, []).append((index, text))
            matched = True
        if matched:
            continue

        part_match = re.fullmatch(r"(.+)\.part(\d{1,3})(\.[^.]+)?", text, re.IGNORECASE)
        if part_match:
            base = part_match.group(1) + (part_match.group(3) or "")
            index = int(part_match.group(2))
            groups.setdefault(base, []).append((index, text))
            continue

        zip_match = re.fullmatch(r"(.+\.zip)\.z(\d{2,3})", text, re.IGNORECASE)
        if zip_match:
            base = zip_match.group(1)
            index = int(zip_match.group(2))
            groups.setdefault(base, []).append((index, text))
            continue

    normalized = {}
    for base, items in groups.items():
        ordered = sorted(items, key=lambda item: item[0])
        normalized[base] = tuple(name for _index, name in ordered)
    return normalized


def _infer_split_asset_base_name(release, preferred_asset_name=""):
    preferred = str(preferred_asset_name or "").strip().lower()
    groups = _split_asset_groups(release)
    if not groups:
        return ""
    if preferred:
        for base in groups:
            if base.lower() == preferred:
                return base
    ranked = sorted(
        groups.items(),
        key=lambda item: (
            -(len(item[1])),
            0 if APP_NAME.lower() in item[0].lower() else 1,
            item[0].lower(),
        ),
    )
    return ranked[0][0]


def _quote_path_part(value):
    return urllib.parse.quote(str(value or "").strip(), safe="")


def _gitee_candidate_tags(update_info=None, tag_name=""):
    version = getattr(update_info, "version", "") if update_info is not None else ""
    candidates = [
        getattr(update_info, "gitee_release_tag", "") if update_info is not None else "",
        tag_name,
        getattr(update_info, "tag_name", "") if update_info is not None else "",
        f"v{version}" if version else "",
        version,
    ]
    tags = []
    for tag in candidates:
        text = str(tag or "").strip()
        if text and text not in tags:
            tags.append(text)
    return tags


def _source_label(source):
    normalized = str(source or "").strip().lower()
    if normalized == UPDATE_SOURCE_GITEE:
        return "Gitee 国内源"
    if normalized == UPDATE_SOURCE_GITHUB:
        return "GitHub 官方源"
    return "自动源"


def _gitee_latest_release_api_url():
    owner, repo = _gitee_owner_repo()
    if not owner or not repo:
        return ""
    return f"https://gitee.com/api/v5/repos/{owner}/{repo}/releases/latest"


def _gitee_manifest_url_for_tag(tag_name):
    repo_url = _gitee_repository_url()
    tag_name = str(tag_name or "").strip()
    if not repo_url or not tag_name:
        return ""
    return f"{repo_url}/releases/download/{tag_name}/latest.json"


def _gitee_release_page_url(tag_name):
    repo_url = _gitee_repository_url()
    tag_name = str(tag_name or "").strip()
    if not repo_url or not tag_name:
        return ""
    return f"{repo_url}/releases/tag/{_quote_path_part(tag_name)}"


def _gitee_release_download_url(tag_name, asset_name):
    repo_url = _gitee_repository_url()
    tag_name = str(tag_name or "").strip()
    asset_name = str(asset_name or "").strip()
    if not repo_url or not tag_name or not asset_name:
        return ""
    return f"{repo_url}/releases/download/{_quote_path_part(tag_name)}/{_quote_path_part(asset_name)}"


def _gitee_release_api_file_download_url(tag_name, asset_name):
    owner, repo = _gitee_owner_repo()
    tag_name = str(tag_name or "").strip()
    asset_name = str(asset_name or "").strip()
    if not owner or not repo or not tag_name or not asset_name:
        return ""
    return (
        f"https://gitee.com/api/v5/repos/{_quote_path_part(owner)}/{_quote_path_part(repo)}"
        f"/releases/{_quote_path_part(tag_name)}/attach_files/{_quote_path_part(asset_name)}/download"
    )


def _gitee_release_attach_files_api_url(release_id):
    owner, repo = _gitee_owner_repo()
    release_id = str(release_id or "").strip()
    if not owner or not repo or not release_id:
        return ""
    return (
        f"https://gitee.com/api/v5/repos/{_quote_path_part(owner)}/{_quote_path_part(repo)}"
        f"/releases/{_quote_path_part(release_id)}/attach_files"
    )


def _gitee_release_attach_file_id_download_url(release_id, attach_file_id):
    owner, repo = _gitee_owner_repo()
    release_id = str(release_id or "").strip()
    attach_file_id = str(attach_file_id or "").strip()
    if not owner or not repo or not release_id or not attach_file_id:
        return ""
    return (
        f"https://gitee.com/api/v5/repos/{_quote_path_part(owner)}/{_quote_path_part(repo)}"
        f"/releases/{_quote_path_part(release_id)}/attach_files/{_quote_path_part(attach_file_id)}/download"
    )


def _gitee_attach_file_download_url(asset):
    repo_url = _gitee_repository_url()
    attach_id = str(asset.get("id") or "").strip() if isinstance(asset, dict) else ""
    if not repo_url or not attach_id:
        return ""
    return f"{repo_url}/attach_files/{_quote_path_part(attach_id)}/download"


def _select_release_asset(release, version):
    assets = release.get("assets") or []
    expected = _expected_asset_name(version).lower()
    fallback = None
    for asset in assets:
        name = str(asset.get("name") or "")
        lower_name = name.lower()
        if lower_name == expected:
            return asset
        if (
            fallback is None
            and lower_name.endswith(".zip")
            and APP_NAME.lower() in lower_name
            and "windows" in lower_name
        ):
            fallback = asset
    return fallback


def _github_manifest_candidates():
    config = _read_update_config()
    configured_urls = _merge_urls(
        _config_url_list(config, "update_github_manifest_urls", "github_manifest_urls", "github_manifest_url"),
        _env_url_list("YHO_GITHUB_MANIFEST_URLS"),
    )
    urls = _merge_urls([LATEST_MANIFEST_URL])
    urls.extend(
        mirrored_url(LATEST_MANIFEST_URL, prefix)
        for prefix in _mirror_prefixes()
    )
    return _merge_urls(urls, configured_urls)


def _general_manifest_candidates():
    config = _read_update_config()
    return _merge_urls(
        _config_url_list(config, "update_manifest_urls", "update_manifest_url"),
        _env_url_list("YHO_UPDATE_MANIFEST_URLS"),
    )


def _gitee_manifest_candidates():
    config = _read_update_config()
    return _merge_urls(
        _config_url_list(config, "update_gitee_manifest_urls", "gitee_manifest_urls", "gitee_manifest_url"),
        _env_url_list("YHO_GITEE_MANIFEST_URLS"),
    )


def _latest_manifest_candidates():
    urls = _merge_urls(_github_manifest_candidates(), _general_manifest_candidates(), _gitee_manifest_candidates())
    api_url = _gitee_latest_release_api_url()
    if api_url:
        urls.append(api_url)
    return _merge_urls(urls)


def _load_manifest_from_urls(urls, timeout=8, source=UPDATE_SOURCE_GITHUB):
    errors = []
    for url in urls:
        try:
            manifest = _load_json(url, timeout=timeout, label="更新清单", api=False)
            if isinstance(manifest, dict):
                manifest.setdefault("source", source)
            return manifest
        except NoPublishedRelease:
            return None
        except ManifestUnavailable:
            continue
        except UpdateError as exc:
            errors.append(str(exc))
    if errors:
        raise UpdateError("无法获取更新清单：" + "；".join(errors[-3:]))
    return None


def _load_github_latest_manifest(timeout=8):
    return _load_manifest_from_urls(
        _merge_urls(_github_manifest_candidates(), _general_manifest_candidates()),
        timeout=timeout,
        source=UPDATE_SOURCE_GITHUB,
    )


def _asset_url_from_release_asset(asset):
    if not isinstance(asset, dict):
        return ""
    for key in ("browser_download_url", "download_url", "url"):
        value = str(asset.get(key) or "").strip()
        if value and value.startswith("http"):
            return value
    return ""


def _load_gitee_release_attach_files(release_id, timeout=8):
    api_url = _gitee_release_attach_files_api_url(release_id)
    if not api_url:
        return []
    payload = _load_json(api_url, timeout=timeout, label="Gitee Release 附件列表", api=False)
    return payload if isinstance(payload, list) else []


def _release_asset_download_urls(release, filename, tag_name="", release_id=""):
    expected = str(filename or "").strip().lower()
    if not expected:
        return []
    urls = []
    resolved_release_id = str(release_id or getattr(release, "get", lambda _k, _d=None: _d)("id") or "").strip()
    for asset in _extract_release_assets(release):
        name = _asset_name_from_release_asset(asset).lower()
        if name != expected:
            continue
        for url in (
            _asset_url_from_release_asset(asset),
            _gitee_attach_file_download_url(asset),
        ):
            if url and url not in urls:
                urls.append(url)
    for tag in _gitee_candidate_tags(tag_name=tag_name):
        url = _gitee_release_download_url(tag, filename)
        if url and url not in urls:
            urls.append(url)
    return urls


def _extract_release_assets(release):
    if not isinstance(release, dict):
        return []
    assets = []
    for key in ("assets", "attach_files", "attachments"):
        raw = release.get(key)
        if isinstance(raw, list):
            assets.extend(item for item in raw if isinstance(item, dict))
    return assets


def _find_release_asset_url(release, filename):
    urls = _release_asset_download_urls(release, filename, tag_name=str(release.get("tag_name") or release.get("tag") or ""))
    return urls[0] if urls else ""


def _infer_release_asset_parts(release, asset_name, tag_name="", release_id=""):
    asset_name = str(asset_name or "").strip()
    if not asset_name:
        asset_name = _infer_split_asset_base_name(release)
        if not asset_name:
            return ()

    assets_by_name = {}
    for asset in _extract_release_assets(release):
        name = _asset_name_from_release_asset(asset)
        if name and name not in assets_by_name:
            assets_by_name[name] = asset

    def build_parts(names):
        parts = []
        for name in names:
            asset = assets_by_name.get(name)
            part = {
                "name": name,
                "source": UPDATE_SOURCE_GITEE,
                "gitee_download_urls": tuple(
                    _release_asset_download_urls(release, name, tag_name=tag_name, release_id=release_id)
                ),
            }
            if isinstance(asset, dict):
                digest = str(asset.get("sha256") or asset.get("digest") or "").strip()
                if digest:
                    part["sha256"] = digest
                try:
                    size = int(asset.get("size") or 0)
                except (TypeError, ValueError):
                    size = 0
                if size > 0:
                    part["size"] = size
            parts.append(part)
        return tuple(parts)

    grouped = _split_asset_groups(release)
    for base, names in grouped.items():
        if base.lower() == asset_name.lower() and len(names) >= 2:
            ordered = list(names)
            if asset_name in assets_by_name and asset_name not in ordered:
                ordered.append(asset_name)
            return build_parts(ordered)

    fallback_base = _infer_split_asset_base_name(release, preferred_asset_name=asset_name)
    fallback_names = grouped.get(fallback_base, ())
    if fallback_base and len(fallback_names) >= 2:
        ordered = list(fallback_names)
        if asset_name in assets_by_name and asset_name not in ordered:
            ordered.append(asset_name)
        return build_parts(ordered)

    return ()


def _load_gitee_manifest_from_latest_release(timeout=8):
    api_url = _gitee_latest_release_api_url()
    if not api_url:
        return None
    release = _load_json(api_url, timeout=timeout, label="Gitee Release 信息", api=False)
    if not isinstance(release, dict):
        return None
    release_id = str(release.get("id") or "").strip()
    tag_name = str(release.get("tag_name") or release.get("tag") or "").strip()
    if not tag_name:
        tag = release.get("tag")
        if isinstance(tag, dict):
            tag_name = str(tag.get("name") or "").strip()
    if not tag_name:
        tag_name = str(release.get("name") or "").strip()
    attach_files = []
    if release_id:
        try:
            attach_files = _load_gitee_release_attach_files(release_id, timeout=timeout)
        except UpdateError:
            attach_files = []
    release_payload = dict(release)
    if attach_files:
        release_payload["attach_files"] = attach_files
    manifest_urls = _merge_urls(
        _release_asset_download_urls(release_payload, "latest.json", tag_name=tag_name, release_id=release_id),
        [_gitee_manifest_url_for_tag(tag_name)],
    )
    manifest = _load_manifest_from_urls(manifest_urls, timeout=timeout, source=UPDATE_SOURCE_GITEE)
    if isinstance(manifest, dict):
        release_version = _version_from_tag(tag_name)
        manifest.setdefault("tag", tag_name)
        manifest.setdefault("tag_name", tag_name)
        manifest["gitee_release_tag"] = tag_name
        manifest["gitee_release_asset_names"] = _release_asset_names(release_payload)
        manifest.setdefault("gitee_html_url", str(release.get("html_url") or _gitee_release_page_url(tag_name) or _gitee_repository_url()))
        manifest.setdefault("html_url", str(release.get("html_url") or _gitee_release_page_url(tag_name) or _gitee_repository_url()))
        version = _version_from_tag(manifest.get("version") or manifest.get("tag") or manifest.get("tag_name"))
        asset_name = str(manifest.get("asset_name") or _expected_asset_name(version)).strip() if version else ""
        if asset_name:
            manifest["gitee_download_urls"] = _merge_urls(
                manifest.get("gitee_download_urls") or manifest.get("gitee_asset_urls"),
                _release_asset_download_urls(release_payload, asset_name, tag_name=tag_name, release_id=release_id),
            )
        part_source = manifest.get("gitee_asset_parts") or manifest.get("gitee_parts") or manifest.get("asset_parts") or manifest.get("parts")
        parts = []
        for part in _coerce_asset_parts(part_source, version=version, tag_name=str(manifest.get("tag") or tag_name), default_source=UPDATE_SOURCE_GITEE):
            parts.append(
                _merge_part_urls(
                    part,
                    _release_asset_download_urls(release_payload, part["name"], tag_name=tag_name, release_id=release_id),
                )
            )
        if not parts and asset_name:
            parts = list(_infer_release_asset_parts(release_payload, asset_name, tag_name=tag_name, release_id=release_id))
        if parse_version(release_version) > parse_version(version or "0"):
            inferred_base = _infer_split_asset_base_name(release_payload, preferred_asset_name=asset_name)
            fallback_parts = list(parts) if parts else list(
                _infer_release_asset_parts(release_payload, inferred_base, tag_name=tag_name, release_id=release_id)
            )
            if inferred_base and fallback_parts:
                manifest["version"] = release_version
                manifest["tag"] = tag_name
                manifest["tag_name"] = tag_name
                manifest["asset_name"] = inferred_base
                manifest["download_url"] = ""
                manifest["download_urls"] = []
                manifest["gitee_download_urls"] = _release_asset_download_urls(
                    release_payload,
                    inferred_base,
                    tag_name=tag_name,
                    release_id=release_id,
                )
                manifest["sha256"] = ""
                manifest["digest"] = ""
                manifest["notes"] = str(manifest.get("notes") or release.get("body") or manifest.get("body") or "")
                asset_name = inferred_base
                version = release_version
                parts = fallback_parts
        if parts:
            manifest["gitee_asset_parts"] = parts
    return manifest


def _load_gitee_latest_manifest(timeout=8):
    configured = _load_manifest_from_urls(_gitee_manifest_candidates(), timeout=timeout, source=UPDATE_SOURCE_GITEE)
    if configured is not None:
        return configured
    return _load_gitee_manifest_from_latest_release(timeout=timeout)


def _normalize_asset_part(item, version="", tag_name="", default_source=UPDATE_SOURCE_AUTO):
    if isinstance(item, str):
        name = item.strip()
        raw = {}
    elif isinstance(item, dict):
        raw = item
        name = str(raw.get("name") or raw.get("asset_name") or raw.get("file_name") or raw.get("filename") or "").strip()
    else:
        return None
    if not name:
        return None
    urls = []
    for key in ("download_url", "url", "browser_download_url"):
        value = _format_url_template(raw.get(key), version=version, tag_name=tag_name, asset_name=name) if isinstance(raw, dict) else ""
        if value and value not in urls:
            urls.append(value)
    for url in _coerce_url_list(raw.get("download_urls") or raw.get("asset_urls") if isinstance(raw, dict) else None):
        formatted = _format_url_template(url, version=version, tag_name=tag_name, asset_name=name)
        if formatted and formatted not in urls:
            urls.append(formatted)
    gitee_urls = []
    for url in _coerce_url_list(raw.get("gitee_download_urls") or raw.get("gitee_asset_urls") if isinstance(raw, dict) else None):
        formatted = _format_url_template(url, version=version, tag_name=tag_name, asset_name=name)
        if formatted and formatted not in gitee_urls:
            gitee_urls.append(formatted)
    github_urls = []
    for url in _coerce_url_list(raw.get("github_download_urls") or raw.get("github_asset_urls") if isinstance(raw, dict) else None):
        formatted = _format_url_template(url, version=version, tag_name=tag_name, asset_name=name)
        if formatted and formatted not in github_urls:
            github_urls.append(formatted)
    return {
        "name": name,
        "sha256": str(raw.get("sha256") or raw.get("digest") or "").strip() if isinstance(raw, dict) else "",
        "size": int(raw.get("size") or 0) if isinstance(raw, dict) and str(raw.get("size") or "").isdigit() else 0,
        "download_urls": tuple(urls),
        "gitee_download_urls": tuple(gitee_urls),
        "github_download_urls": tuple(github_urls),
        "source": str(raw.get("source") or default_source) if isinstance(raw, dict) else default_source,
    }


def _coerce_asset_parts(value, version="", tag_name="", default_source=UPDATE_SOURCE_AUTO):
    if not value:
        return ()
    items = value if isinstance(value, (list, tuple)) else [value]
    parts = []
    seen = set()
    for item in items:
        part = _normalize_asset_part(item, version=version, tag_name=tag_name, default_source=default_source)
        if not part or part["name"] in seen:
            continue
        seen.add(part["name"])
        parts.append(part)
    return tuple(parts)


def _merge_part_urls(part, *url_groups):
    merged = list(part.get("gitee_download_urls") or ())
    for group in url_groups:
        for url in _coerce_url_list(group):
            if url and url not in merged:
                merged.append(url)
    updated = dict(part)
    updated["gitee_download_urls"] = tuple(merged)
    return updated


def _manifest_to_update_info(manifest, current_version=APP_VERSION, source=None):
    if not isinstance(manifest, dict):
        raise UpdateError("更新清单格式错误：根节点不是 JSON 对象")
    manifest_source = str(source or manifest.get("source") or UPDATE_SOURCE_GITHUB).strip().lower()
    if manifest_source not in {UPDATE_SOURCE_GITHUB, UPDATE_SOURCE_GITEE, UPDATE_SOURCE_AUTO}:
        manifest_source = UPDATE_SOURCE_GITHUB

    version = _version_from_tag(manifest.get("version") or manifest.get("tag") or manifest.get("tag_name"))
    if not version:
        raise UpdateError("更新清单缺少 version")
    if not is_newer_version(version, current_version):
        return None

    tag_name = str(manifest.get("tag") or manifest.get("tag_name") or f"v{version}").strip()
    asset_name = str(manifest.get("asset_name") or _expected_asset_name(version)).strip()
    if not asset_name:
        raise UpdateError("更新清单缺少 asset_name")

    download_url = str(manifest.get("download_url") or "").strip()
    if manifest_source == UPDATE_SOURCE_GITEE:
        if not download_url or _is_github_url(download_url):
            download_url = ""
    elif not download_url or _is_repo_release_download_url(download_url):
        download_url = _latest_asset_download_url(asset_name)
    download_urls = []
    for item in _coerce_url_list(manifest.get("download_urls") or manifest.get("asset_urls")):
        formatted = _format_url_template(item, version=version, tag_name=tag_name, asset_name=asset_name)
        if formatted and formatted not in download_urls:
            download_urls.append(formatted)
    if download_url not in download_urls:
        download_urls.append(download_url)
    github_download_urls = []
    for item in _coerce_url_list(manifest.get("github_download_urls") or manifest.get("github_asset_urls")):
        formatted = _format_url_template(item, version=version, tag_name=tag_name, asset_name=asset_name)
        if formatted and formatted not in github_download_urls:
            github_download_urls.append(formatted)
    gitee_download_urls = []
    for item in _coerce_url_list(manifest.get("gitee_download_urls") or manifest.get("gitee_asset_urls")):
        formatted = _format_url_template(item, version=version, tag_name=tag_name, asset_name=asset_name)
        if formatted and formatted not in gitee_download_urls:
            gitee_download_urls.append(formatted)
    asset_parts = _coerce_asset_parts(
        manifest.get("asset_parts") or manifest.get("parts"),
        version=version,
        tag_name=tag_name,
        default_source=manifest_source,
    )
    gitee_asset_parts = _coerce_asset_parts(
        manifest.get("gitee_asset_parts") or manifest.get("gitee_parts"),
        version=version,
        tag_name=str(manifest.get("gitee_release_tag") or tag_name),
        default_source=UPDATE_SOURCE_GITEE,
    )
    if manifest_source == UPDATE_SOURCE_GITEE:
        gitee_html_url = str(
            manifest.get("gitee_html_url")
            or manifest.get("html_url")
            or _gitee_release_page_url(str(manifest.get("gitee_release_tag") or tag_name))
            or _gitee_repository_url()
            or f"{APP_REPOSITORY_URL}/releases/latest"
        )
        github_html_url = str(
            manifest.get("github_html_url")
            or f"{APP_REPOSITORY_URL}/releases/latest"
        )
        html_url = str(
            gitee_html_url
        )
    else:
        github_html_url = str(
            manifest.get("github_html_url")
            or manifest.get("html_url")
            or f"{APP_REPOSITORY_URL}/releases/latest"
        )
        gitee_html_url = str(
            manifest.get("gitee_html_url")
            or _gitee_release_page_url(str(manifest.get("gitee_release_tag") or tag_name))
            or _gitee_repository_url()
            or ""
        )
        html_url = str(
            github_html_url
        )

    return UpdateInfo(
        version=version,
        tag_name=tag_name,
        release_name=str(manifest.get("release_name") or tag_name),
        body=str(manifest.get("notes") or manifest.get("body") or ""),
        asset_name=asset_name,
        download_url=download_url,
        html_url=html_url,
        github_html_url=github_html_url,
        gitee_html_url=gitee_html_url,
        digest=str(manifest.get("digest") or manifest.get("sha256") or ""),
        github_digest=str(manifest.get("github_digest") or manifest.get("github_sha256") or ""),
        gitee_digest=str(manifest.get("gitee_digest") or manifest.get("gitee_sha256") or ""),
        download_urls=tuple(download_urls),
        github_download_urls=tuple(github_download_urls),
        gitee_download_urls=tuple(gitee_download_urls),
        asset_parts=asset_parts,
        gitee_asset_parts=gitee_asset_parts,
        gitee_release_tag=str(manifest.get("gitee_release_tag") or ""),
        gitee_release_asset_names=tuple(_coerce_url_list(manifest.get("gitee_release_asset_names"))),
        source=manifest_source,
    )


def _merge_source_update_info(primary, secondary):
    if primary is None or secondary is None:
        return primary
    if parse_version(primary.version) != parse_version(secondary.version):
        return primary
    return replace(
        primary,
        gitee_download_urls=tuple(_merge_urls(primary.gitee_download_urls, secondary.gitee_download_urls)),
        gitee_asset_parts=tuple(secondary.gitee_asset_parts or primary.gitee_asset_parts),
        gitee_release_tag=secondary.gitee_release_tag or primary.gitee_release_tag,
        gitee_release_asset_names=tuple(
            _merge_urls(primary.gitee_release_asset_names, secondary.gitee_release_asset_names)
        ),
        gitee_html_url=secondary.gitee_html_url or primary.gitee_html_url,
        gitee_digest=secondary.gitee_digest or primary.gitee_digest,
    )


def _try_enrich_with_gitee(update_info, current_version=APP_VERSION, timeout=8):
    if update_info is None:
        return None
    try:
        manifest = _load_gitee_latest_manifest(timeout=timeout)
        if manifest is None:
            return update_info
        gitee_info = _manifest_to_update_info(
            manifest,
            current_version=current_version,
            source=UPDATE_SOURCE_GITEE,
        )
        return _merge_source_update_info(update_info, gitee_info)
    except UpdateError:
        return update_info


def check_for_update(current_version=APP_VERSION, timeout=8):
    errors = []
    github_manifest_loaded = False
    gitee_manifest_loaded = False
    try:
        manifest = _load_github_latest_manifest(timeout=timeout)
        github_manifest_loaded = manifest is not None
        if manifest is not None:
            update_info = _manifest_to_update_info(
                manifest,
                current_version=current_version,
                source=UPDATE_SOURCE_GITHUB,
            )
            if update_info is not None:
                return _try_enrich_with_gitee(update_info, current_version=current_version, timeout=timeout)
    except UpdateError as exc:
        errors.append(str(exc))

    try:
        manifest = _load_gitee_latest_manifest(timeout=timeout)
        gitee_manifest_loaded = manifest is not None
        if manifest is not None:
            update_info = _manifest_to_update_info(
                manifest,
                current_version=current_version,
                source=UPDATE_SOURCE_GITEE,
            )
            if update_info is not None:
                return update_info
    except UpdateError as exc:
        errors.append(str(exc))

    if errors and not (github_manifest_loaded or gitee_manifest_loaded):
        raise UpdateError("；".join(errors[-3:]))

    if not ENABLE_GITHUB_API_FALLBACK:
        return None

    try:
        release = _load_json(LATEST_RELEASE_API, timeout=timeout, api=True)
    except NoPublishedRelease:
        return None
    tag_name = str(release.get("tag_name") or "")
    remote_version = _version_from_tag(tag_name)
    if not remote_version or not is_newer_version(remote_version, current_version):
        return None

    asset = _select_release_asset(release, remote_version)
    if not asset:
        raise UpdateError(f"最新版本 v{remote_version} 未找到 Windows zip 发布包")

    download_url = asset.get("browser_download_url")
    if not download_url:
        raise UpdateError("最新版本发布包缺少下载地址")

    return UpdateInfo(
        version=remote_version,
        tag_name=tag_name,
        release_name=str(release.get("name") or tag_name),
        body=str(release.get("body") or ""),
        asset_name=str(asset.get("name") or _expected_asset_name(remote_version)),
        download_url=str(download_url),
        html_url=str(release.get("html_url") or APP_REPOSITORY_URL),
        github_html_url=str(release.get("html_url") or APP_REPOSITORY_URL),
        digest=str(asset.get("digest") or ""),
        github_digest=str(asset.get("digest") or ""),
    )


def mirrored_url(url, prefix):
    prefix = (prefix or "").strip()
    if not prefix:
        return url
    return f"{prefix.rstrip('/')}/{url}"


def _configured_download_urls(update_info, asset_name=None):
    config = _read_update_config()
    name = asset_name or update_info.asset_name
    urls = _merge_urls(
        _config_url_list(config, "update_download_urls", "update_download_url"),
        _env_url_list("YHO_UPDATE_DOWNLOAD_URLS"),
    )
    return [
        _format_url_template(url, version=update_info.version, tag_name=update_info.tag_name, asset_name=name)
        for url in urls
    ]


def _configured_source_download_urls(update_info, source, asset_name=None):
    config = _read_update_config()
    name = asset_name or update_info.asset_name
    if source == UPDATE_SOURCE_GITEE:
        urls = _merge_urls(
            _config_url_list(config, "update_gitee_download_urls", "gitee_download_urls", "gitee_download_url"),
            _env_url_list("YHO_GITEE_DOWNLOAD_URLS"),
        )
    elif source == UPDATE_SOURCE_GITHUB:
        urls = _merge_urls(
            _config_url_list(config, "update_github_download_urls", "github_download_urls", "github_download_url"),
            _env_url_list("YHO_GITHUB_DOWNLOAD_URLS"),
        )
    else:
        urls = []
    return [
        _format_url_template(url, version=update_info.version, tag_name=update_info.tag_name, asset_name=name)
        for url in urls
    ]


def _gitee_fallback_download_urls(update_info, asset_name=None):
    name = asset_name or update_info.asset_name
    urls = []
    for tag in _gitee_candidate_tags(update_info):
        url = _gitee_release_download_url(tag, name)
        if url and url not in urls:
            urls.append(url)
    return urls


def _get_asset_parts(update_info, source):
    if source == UPDATE_SOURCE_GITEE:
        return tuple(getattr(update_info, "gitee_asset_parts", ()) or getattr(update_info, "asset_parts", ()))
    if source == UPDATE_SOURCE_GITHUB:
        return tuple(getattr(update_info, "asset_parts", ()))
    if getattr(update_info, "gitee_asset_parts", ()):
        return tuple(getattr(update_info, "gitee_asset_parts", ()))
    return tuple(getattr(update_info, "asset_parts", ()))


def get_download_candidates(update_info, source=UPDATE_SOURCE_GITHUB):
    source = str(source or UPDATE_SOURCE_GITHUB).strip().lower()
    if source not in {UPDATE_SOURCE_GITHUB, UPDATE_SOURCE_GITEE, UPDATE_SOURCE_AUTO}:
        source = UPDATE_SOURCE_GITHUB

    if source == UPDATE_SOURCE_GITEE:
        if _get_asset_parts(update_info, UPDATE_SOURCE_GITEE):
            return ()
        generic_non_github = [
            url for url in _merge_urls(_configured_download_urls(update_info), getattr(update_info, "download_urls", ()))
            if not _is_github_url(url)
        ]
        candidates = _merge_urls(
            _configured_source_download_urls(update_info, UPDATE_SOURCE_GITEE),
            getattr(update_info, "gitee_download_urls", ()),
            _gitee_fallback_download_urls(update_info),
            generic_non_github,
        )
        return tuple(url for url in candidates if url and not _is_gitee_api_download_url(url))

    if source == UPDATE_SOURCE_AUTO:
        candidates = _merge_urls(
            _configured_download_urls(update_info),
            _configured_source_download_urls(update_info, UPDATE_SOURCE_GITHUB),
            getattr(update_info, "github_download_urls", ()),
            getattr(update_info, "download_urls", ()),
            [update_info.download_url, _latest_asset_download_url(update_info.asset_name)],
            _configured_source_download_urls(update_info, UPDATE_SOURCE_GITEE),
            getattr(update_info, "gitee_download_urls", ()),
            _gitee_fallback_download_urls(update_info),
        )
        return tuple(url for url in candidates if url and not _is_gitee_api_download_url(url))

    candidates = _merge_urls(
        _configured_source_download_urls(update_info, UPDATE_SOURCE_GITHUB),
        getattr(update_info, "github_download_urls", ()),
        [update_info.download_url, _latest_asset_download_url(update_info.asset_name)],
        _configured_download_urls(update_info),
        getattr(update_info, "download_urls", ()),
    )
    return tuple(url for url in candidates if url)


def _part_download_candidates(update_info, part, source):
    name = str(part.get("name") or "").strip()
    if not name:
        return ()
    part_source = str(part.get("source") or source or UPDATE_SOURCE_AUTO).strip().lower()
    if part_source not in {UPDATE_SOURCE_GITHUB, UPDATE_SOURCE_GITEE, UPDATE_SOURCE_AUTO}:
        part_source = source

    if part_source == UPDATE_SOURCE_GITEE:
        return tuple(
            url for url in _merge_urls(
                _configured_source_download_urls(update_info, UPDATE_SOURCE_GITEE, asset_name=name),
                part.get("gitee_download_urls", ()),
                _gitee_fallback_download_urls(update_info, asset_name=name),
                _configured_download_urls(update_info, asset_name=name),
                part.get("download_urls", ()),
            )
            if url and not _is_gitee_api_download_url(url)
        )

    if part_source == UPDATE_SOURCE_GITHUB:
        return tuple(
            url for url in _merge_urls(
                _configured_source_download_urls(update_info, UPDATE_SOURCE_GITHUB, asset_name=name),
                part.get("github_download_urls", ()),
                _configured_download_urls(update_info, asset_name=name),
                part.get("download_urls", ()),
                [_latest_asset_download_url(name)],
            )
            if url
        )

    return tuple(
        url for url in _merge_urls(
            part.get("download_urls", ()),
            part.get("gitee_download_urls", ()),
            part.get("github_download_urls", ()),
            _configured_download_urls(update_info, asset_name=name),
            _configured_source_download_urls(update_info, UPDATE_SOURCE_GITEE, asset_name=name),
            _configured_source_download_urls(update_info, UPDATE_SOURCE_GITHUB, asset_name=name),
            _gitee_fallback_download_urls(update_info, asset_name=name),
            [_latest_asset_download_url(name)],
        )
        if url and not _is_gitee_api_download_url(url)
    )


def _download_once(url, target_path, progress_callback=None, timeout=20, cancel_callback=None):
    _raise_if_cancelled(cancel_callback)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout, context=_ssl_context_with_certifi()) as response:
        total = int(response.headers.get("Content-Length") or 0)
        downloaded = 0
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with open(target_path, "wb") as file:
            while True:
                _raise_if_cancelled(cancel_callback)
                chunk = response.read(1024 * 256)
                if not chunk:
                    break
                file.write(chunk)
                downloaded += len(chunk)
                if progress_callback:
                    percent = int(downloaded * 100 / total) if total else 0
                    progress_callback(max(0, min(100, percent)), downloaded, total)
    _raise_if_cancelled(cancel_callback)
    if progress_callback:
        progress_callback(100, target_path.stat().st_size, target_path.stat().st_size)


def _verify_sha256(path, expected_sha256):
    expected = (expected_sha256 or "").strip().lower()
    if not expected:
        return
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    actual = digest.hexdigest().lower()
    if actual != expected:
        raise UpdateError("更新包 SHA256 校验失败，已拒绝安装")


def _looks_like_forbidden_download_error(error):
    text = str(error or "").lower()
    return "403" in text or "forbidden" in text


def _download_split_update(update_info, parts, download_root, progress_callback=None, timeout=25, source=UPDATE_SOURCE_GITEE, cancel_callback=None):
    parts_root = download_root / f"{update_info.asset_name}.parts"
    parts_root.mkdir(parents=True, exist_ok=True)
    target_path = download_root / update_info.asset_name
    downloaded_parts = []
    total_parts = max(1, len(parts))
    expected_sha256 = _expected_sha256_for_source(update_info, source)

    try:
        for part_index, part in enumerate(parts, start=1):
            _raise_if_cancelled(cancel_callback)
            part_name = str(part.get("name") or "").strip()
            if not part_name:
                raise UpdateError("分卷更新配置缺少文件名")
            part_path = parts_root / part_name
            candidates = list(_part_download_candidates(update_info, part, source=source))
            if not candidates:
                raise UpdateError(f"分卷附件缺少下载地址：{part_name}")

            errors = []
            for candidate_index, url in enumerate(candidates):
                try:
                    if part_path.exists():
                        part_path.unlink()

                    def on_part_progress(percent, downloaded, total):
                        if progress_callback is None:
                            return
                        start = (part_index - 1) / total_parts
                        end = part_index / total_parts
                        mapped = int((start + (end - start) * (max(0, min(100, percent)) / 100.0)) * 100)
                        progress_callback(mapped, downloaded, total)

                    _download_once(url, part_path, progress_callback=on_part_progress, timeout=timeout, cancel_callback=cancel_callback)
                    _verify_sha256(part_path, part.get("sha256"))
                    downloaded_parts.append(part_path)
                    break
                except DownloadCancelled:
                    raise
                except Exception as exc:
                    errors.append(str(exc))
                    try:
                        if part_path.exists():
                            part_path.unlink()
                    except OSError:
                        pass
                    if candidate_index == 0 and progress_callback:
                        progress_callback(int(((part_index - 1) / total_parts) * 100), 0, 0)
            else:
                raise UpdateError(f"分卷附件下载失败 {part_name}：" + "；".join(errors[-3:]))

        with open(target_path, "wb") as merged:
            for part_path in downloaded_parts:
                _raise_if_cancelled(cancel_callback)
                with open(part_path, "rb") as source_file:
                    shutil.copyfileobj(source_file, merged, length=1024 * 1024)

        _verify_sha256(target_path, expected_sha256)
        if progress_callback:
            progress_callback(100, target_path.stat().st_size, target_path.stat().st_size)
        shutil.rmtree(parts_root, ignore_errors=True)
        return str(target_path)
    except Exception:
        try:
            if target_path.exists():
                target_path.unlink()
        except OSError:
            pass
        shutil.rmtree(parts_root, ignore_errors=True)
        raise


def download_update(update_info, progress_callback=None, timeout=25, source=UPDATE_SOURCE_GITHUB, cancel_callback=None):
    if update_info is None:
        raise UpdateError("没有可下载的更新信息")

    # Direct URL download: bypass GitHub/Gitee candidates
    if str(source or "").strip().lower() == "direct":
        direct_url = str(getattr(update_info, "download_url", "") or "").strip()
        if not direct_url:
            raise UpdateError("直连下载地址为空")
        download_root = _update_subdir(UPDATE_DOWNLOAD_DIR_NAME)
        _cleanup_old_children(download_root, max_age_seconds=86400)
        filename = direct_url.rsplit("/", 1)[-1].split("?")[0] or "update.zip"
        target_path = download_root / filename
        _raise_if_cancelled(cancel_callback)
        _download_once(direct_url, target_path, progress_callback=progress_callback, timeout=timeout, cancel_callback=cancel_callback)
        return str(target_path)

    download_root = _update_subdir(UPDATE_DOWNLOAD_DIR_NAME)
    _cleanup_old_children(download_root, max_age_seconds=86400)
    target_path = download_root / update_info.asset_name
    expected_sha256 = _expected_sha256_for_source(update_info, source)
    parts = _get_asset_parts(update_info, source)
    _raise_if_cancelled(cancel_callback)

    if parts:
        try:
            return _download_split_update(
                update_info,
                parts,
                download_root=download_root,
                progress_callback=progress_callback,
                timeout=timeout,
                source=source,
                cancel_callback=cancel_callback,
            )
        except UpdateError as exc:
            if source != UPDATE_SOURCE_GITEE or not _looks_like_forbidden_download_error(exc):
                raise
            source = UPDATE_SOURCE_GITHUB
            expected_sha256 = _expected_sha256_for_source(update_info, source)
            if progress_callback:
                progress_callback(0, 0, 0)

    errors = []
    candidates = list(get_download_candidates(update_info, source=source))
    if not candidates:
        available = "、".join(getattr(update_info, "gitee_release_asset_names", ())[:12])
        if source == UPDATE_SOURCE_GITEE and available:
            raise UpdateError(f"{_source_label(source)}未找到匹配的更新包附件。当前 Release 可见附件：{available}")
        raise UpdateError(f"{_source_label(source)}没有可用下载地址")
    if expected_sha256:
        for base_url in list(candidates):
            if not _is_github_url(base_url):
                continue
            for prefix in _mirror_prefixes():
                mirror = mirrored_url(base_url, prefix)
                if mirror not in candidates:
                    candidates.append(mirror)

    for index, url in enumerate(candidates):
        try:
            _raise_if_cancelled(cancel_callback)
            if target_path.exists():
                target_path.unlink()
            _download_once(url, target_path, progress_callback=progress_callback, timeout=timeout, cancel_callback=cancel_callback)
            _verify_sha256(target_path, expected_sha256)
            return str(target_path)
        except DownloadCancelled:
            try:
                if target_path.exists():
                    target_path.unlink()
            except OSError:
                pass
            raise
        except Exception as exc:
            errors.append(str(exc))
            try:
                if target_path.exists():
                    target_path.unlink()
            except OSError:
                pass
            if index == 0 and progress_callback:
                progress_callback(0, 0, 0)

    raise UpdateError(f"{_source_label(source)}更新包下载失败：" + "；".join(errors[-3:]))


def cleanup_old_update_runners(app_dir=None, max_age_seconds=86400):
    _cleanup_old_children(_update_subdir(UPDATE_RUNNER_DIR_NAME, app_dir=app_dir), max_age_seconds=max_age_seconds)


def prepare_updater_runner(app_dir=None):
    app_dir = Path(app_dir or app_base_dir()).resolve()
    updater = app_dir / "YHoUpdater.exe"
    if not updater.exists():
        raise UpdateError("未找到 YHoUpdater.exe，当前版本不支持全自动更新")

    cleanup_old_update_runners(app_dir=app_dir)
    runner_dir = _update_subdir(UPDATE_RUNNER_DIR_NAME, app_dir=app_dir) / str(os.getpid())
    runner_dir.mkdir(parents=True, exist_ok=True)
    runner_path = runner_dir / updater.name
    shutil.copy2(updater, runner_path)
    return runner_path


def start_external_update(package_path, app_dir=None, main_pid=None, version=None):
    app_dir = Path(app_dir or app_base_dir()).resolve()
    package_path = Path(package_path).resolve()
    if not package_path.exists():
        raise UpdateError("更新包不存在，无法启动更新器")

    runner_path = prepare_updater_runner(app_dir)
    args = [
        str(runner_path),
        "--pid",
        str(int(main_pid or os.getpid())),
        "--package",
        str(package_path),
        "--app-dir",
        str(app_dir),
        "--exe",
        f"{APP_NAME}.exe",
    ]
    if version:
        args.extend(["--version", str(version)])
    subprocess.Popen(args, cwd=str(app_dir), close_fds=True)
    return True
