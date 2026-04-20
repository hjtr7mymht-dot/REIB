from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import Dict, List, Optional, Sequence, Tuple

if sys.platform == "win32":
    import winreg  # type: ignore[attr-defined]
else:
    winreg = None


def _run(cmd: Sequence[str], timeout: float = 5.0) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(
            list(cmd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        return (
            proc.returncode,
            proc.stdout.decode("utf-8", errors="ignore"),
            proc.stderr.decode("utf-8", errors="ignore"),
        )
    except FileNotFoundError:
        return 127, "", f"command not found: {cmd[0] if cmd else ''}"
    except Exception as exc:
        return 1, "", str(exc)


def get_mumu_install_from_registry() -> List[str]:
    if winreg is None:
        return []

    roots = [
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
    ]
    names = ("MuMu", "Nemu", "MuMuPlayer")
    found: List[str] = []

    for root in roots:
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root)
        except Exception:
            continue

        with key:
            idx = 0
            while True:
                try:
                    child_name = winreg.EnumKey(key, idx)
                except OSError:
                    break
                idx += 1
                if not any(k.lower() in child_name.lower() for k in names):
                    continue
                try:
                    child = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root + "\\" + child_name)
                    with child:
                        path, _ = winreg.QueryValueEx(child, "InstallLocation")
                        if path and os.path.isdir(path) and path not in found:
                            found.append(path)
                except Exception:
                    continue

    return found


def get_mumu_adb_paths() -> List[str]:
    candidates: List[str] = []
    install_dirs = get_mumu_install_from_registry()
    env_roots = [os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", "")]
    for root in env_roots:
        if root:
            install_dirs.append(os.path.join(root, "Netease"))

    checked = set()
    patterns = (
        ("nx_main", "adb.exe"),
        ("emulator", "nemu", "adb.exe"),
        ("adb_tools", "adb.exe"),
    )

    for base in install_dirs:
        if not base or not os.path.isdir(base):
            continue
        norm = os.path.normpath(base)
        if norm in checked:
            continue
        checked.add(norm)

        for path in _walk_depth_limited(base, max_depth=3):
            for pat in patterns:
                p = os.path.join(path, *pat)
                if os.path.isfile(p):
                    fp = os.path.normpath(p)
                    if fp not in candidates:
                        candidates.append(fp)

    return candidates


def get_adb_candidate_paths(preferred_path: Optional[str] = None) -> List[str]:
    candidates: List[str] = []
    if preferred_path:
        candidates.append(preferred_path)

    workspace_candidates = [
        os.path.join(os.getcwd(), "adb_tools", "adb.exe"),
        os.path.join(os.getcwd(), "platform-tools", "adb.exe"),
    ]
    for item in workspace_candidates:
        if os.path.isfile(item):
            candidates.append(os.path.normpath(item))

    candidates.extend(get_mumu_adb_paths())
    candidates.append("adb")
    return list(dict.fromkeys(candidates))


def select_working_adb_path(preferred_path: Optional[str] = None) -> Tuple[str, str]:
    for candidate in get_adb_candidate_paths(preferred_path):
        code, out, err = _run([candidate, "version"], timeout=3.0)
        if code == 0:
            return candidate, (out.strip() or "adb ok")
    return "adb", "未找到可用 adb，将使用系统 PATH 回退"


def _walk_depth_limited(root: str, max_depth: int = 2) -> List[str]:
    root = os.path.normpath(root)
    output = [root]
    base_depth = root.count(os.sep)
    for current, dirs, _ in os.walk(root):
        depth = current.count(os.sep) - base_depth
        if depth >= max_depth:
            dirs[:] = []
            continue
        output.append(current)
    return output


def _parse_devices_output(text: str) -> List[str]:
    serials: List[str] = []
    for line in text.splitlines()[1:]:
        row = line.strip()
        if not row:
            continue
        parts = row.split()
        if len(parts) >= 2 and parts[1] == "device":
            serials.append(parts[0])
    return serials


def discover_all_serials_and_ports(adb_path: Optional[str] = None) -> List[str]:
    adb, _ = select_working_adb_path(adb_path)

    # 先连接常见本地端口，提高首次发现成功率。
    common_ports = [
        5555,
        7555,
        62001,
        62025,
        62026,
        16384,
        16385,
        16416,
        16448,
    ]
    for p in common_ports:
        serial = f"127.0.0.1:{p}"
        try:
            _run([adb, "connect", serial], timeout=2.0)
        except Exception:
            pass

    code, out, _ = _run([adb, "devices"], timeout=5.0)
    if code != 0:
        return []

    discovered = _parse_devices_output(out)
    # 保证返回唯一且有序。
    return list(dict.fromkeys(discovered))


def discover_device_entries(adb_path: Optional[str] = None) -> Dict[str, object]:
    adb, message = select_working_adb_path(adb_path)
    serials = discover_all_serials_and_ports(adb)
    entries = []
    for serial in serials:
        transport = "WiFi ADB" if ":" in serial else "USB ADB"
        entries.append(
            {
                "serial": serial,
                "label": f"{serial} · {transport}",
                "transport": transport,
                "status": "device",
            }
        )
    return {
        "adb_path": adb,
        "adb_message": message,
        "devices": entries,
    }


def serial_to_nemu_id(serial: str) -> Optional[int]:
    m = re.match(r"^127\.0\.0\.1:(\d+)$", serial.strip())
    if not m:
        return None
    port = int(m.group(1))
    if port < 16384:
        return None
    delta = port - 16384
    if delta % 32 == 0:
        return delta // 32
    return None
