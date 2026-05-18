import hashlib
import hmac
import json
import os
import platform
import secrets
import subprocess
from pathlib import Path

from core.paths import writable_path


INSTALL_ID_FILE = "auth_device.json"


def _default_install_id_path():
    return Path(writable_path(INSTALL_ID_FILE))


def _compute_install_id_hmac(install_id):
    machine_guid = _windows_machine_guid()
    key = machine_guid.encode("utf-8") if machine_guid else b"default-key"
    return hmac.new(key, install_id.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def _read_install_id(path):
    try:
        with open(path, "r", encoding="utf-8") as file:
            data = json.load(file)
        value = str(data.get("install_id", "")).strip()
        if len(value) >= 16:
            expected_hmac = _compute_install_id_hmac(value)
            stored_hmac = str(data.get("hmac", "")).strip()
            if stored_hmac and hmac.compare_digest(stored_hmac, expected_hmac):
                return value
            return ""
    except Exception:
        return ""
    return ""


def _write_install_id(path, install_id):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    data = {
        "install_id": install_id,
        "hmac": _compute_install_id_hmac(install_id),
    }
    with open(tmp_path, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def get_or_create_install_id(path=None, token_factory=None):
    target = Path(path) if path is not None else _default_install_id_path()
    existing = _read_install_id(target)
    if existing:
        return existing
    factory = token_factory or (lambda: secrets.token_urlsafe(24))
    install_id = str(factory()).strip()
    if len(install_id) < 16:
        install_id = secrets.token_urlsafe(24)
    _write_install_id(target, install_id)
    return install_id


def _windows_machine_guid():
    if os.name != "nt":
        return ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
            value, _value_type = winreg.QueryValueEx(key, "MachineGuid")
        return str(value).strip()
    except Exception:
        return ""


def _stable_local_parts(machine_guid=None):
    guid = _windows_machine_guid() if machine_guid is None else str(machine_guid or "")
    return [
        f"platform={platform.system().lower()}",
        f"machine_guid={guid}",
        f"computer={os.environ.get('COMPUTERNAME') or os.environ.get('HOSTNAME') or ''}",
    ]


def build_device_hash(install_id=None, machine_guid=None, extra_parts=None):
    local_install_id = install_id or get_or_create_install_id()
    parts = [
        "YHoAutoFish-device-v1",
        f"install_id={local_install_id}",
        *_stable_local_parts(machine_guid=machine_guid),
    ]
    if extra_parts:
        parts.extend(str(part) for part in extra_parts if part is not None)
    normalized = "\n".join(parts).encode("utf-8", errors="ignore")
    return hashlib.sha256(normalized).hexdigest()


def is_valid_device_hash(value):
    text = str(value or "")
    return len(text) == 64 and all(ch in "0123456789abcdef" for ch in text.lower())


def _wmi_query(wmi_class, property_name):
    if os.name != "nt":
        return ""
    try:
        result = subprocess.run(
            ["wmic", wmi_class, "get", property_name, "/value"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in result.stdout.splitlines():
            line = line.strip()
            if "=" in line:
                key, value = line.split("=", 1)
                if key.strip().lower() == property_name.lower():
                    return value.strip()
    except Exception:
        return ""
    return ""


def _collect_hardware_ids():
    return {
        "bios_uuid": _wmi_query("Win32_ComputerSystemProduct", "UUID"),
        "cpu_id": _wmi_query("Win32_Processor", "ProcessorId"),
        "board_serial": _wmi_query("Win32_BaseBoard", "SerialNumber"),
        "machine_guid": _windows_machine_guid(),
        "computer_name": os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or "",
    }


def build_device_hash_v2(install_id=None, hw_ids=None):
    local_install_id = install_id or get_or_create_install_id()
    if hw_ids is None:
        hw_ids = _collect_hardware_ids()
    parts = [
        "YHoAutoFish-device-v2",
        f"install_id={local_install_id}",
        f"bios_uuid={hw_ids.get('bios_uuid', '')}",
        f"cpu_id={hw_ids.get('cpu_id', '')}",
        f"board_serial={hw_ids.get('board_serial', '')}",
    ]
    normalized = "\n".join(parts).encode("utf-8", errors="ignore")
    return hashlib.sha256(normalized).hexdigest()
