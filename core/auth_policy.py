import re
from urllib.parse import urlparse


_DEFAULT_AUTH_HOSTS = ["106.52.97.207", "auth.fadedai.club"]
_REMOTE_HOSTS: list[str] = []

# Backward compatibility: keep as list for existing imports
AUTH_SERVER_HOSTS = list(_DEFAULT_AUTH_HOSTS)
AUTH_SERVER_IP = _DEFAULT_AUTH_HOSTS[0]
AUTH_PUBLIC_BASE_URL = f"https://{_DEFAULT_AUTH_HOSTS[0]}/api"

_IPV4_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")


def get_effective_hosts() -> list[str]:
    """Default hosts first + remote hosts appended, deduplicated."""
    seen: set[str] = set()
    result: list[str] = []
    for h in _DEFAULT_AUTH_HOSTS + _REMOTE_HOSTS:
        key = h.strip().lower()
        if key and key not in seen:
            seen.add(key)
            result.append(h.strip())
    return result


def get_trusted_auth_hosts() -> set[str]:
    """All hosts that validate_auth_base_url should accept."""
    return {h.strip().lower() for h in get_effective_hosts()}


def _is_valid_host(host):
    """Validate host is a plausible IP or domain name."""
    h = str(host or "").strip()
    if not h or len(h) > 253:
        return False
    if is_ip_host(h):
        return True
    return bool(re.match(
        r"^[a-zA-Z0-9]([a-zA-Z0-9\-]*[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9\-]*[a-zA-Z0-9])?)*$",
        h,
    ))


def update_remote_hosts(hosts: list[str]) -> None:
    """Update remote hosts list (from server response). Excludes defaults, validates format."""
    global _REMOTE_HOSTS
    default_lower = {x.lower() for x in _DEFAULT_AUTH_HOSTS}
    _REMOTE_HOSTS = [
        h.strip() for h in hosts
        if h.strip() and h.strip().lower() not in default_lower and _is_valid_host(h)
    ]


def is_ip_host(host):
    """Return True if *host* looks like an IPv4 address."""
    return bool(_IPV4_RE.match(str(host or "").strip()))


class AuthPolicyError(ValueError):
    pass


def normalize_auth_base_url(value):
    return str(value or "").strip().rstrip("/")


def get_auth_base_url(_config=None):
    return AUTH_PUBLIC_BASE_URL


def validate_auth_base_url(value):
    url = normalize_auth_base_url(value)
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise AuthPolicyError("授权 API 必须使用 HTTPS")
    host = (parsed.hostname or "").lower()
    trusted = get_trusted_auth_hosts()
    if host not in trusted:
        allowed = ", ".join(sorted(trusted))
        raise AuthPolicyError(f"授权 API 域名未写入客户端信任列表: {host or '<empty>'}; allowed={allowed}")
    if not parsed.path.rstrip("/").endswith("/api"):
        raise AuthPolicyError("授权 API 地址必须以 /api 结尾")
    return url
