from __future__ import annotations

import atexit
import os
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

CURRENT_ADB_PATH = ""
_DEFAULT_TIMEOUT = 8.0
_OPEN_CONTROLLERS: List["AdbController"] = []
_OPEN_LOCK = threading.Lock()


def get_bundled_resource_path(relative_path: str) -> str:
    if getattr(sys, "frozen", False):
        if hasattr(sys, "_MEIPASS"):
            base = sys._MEIPASS  # type: ignore[attr-defined]
        else:
            base = os.path.dirname(sys.executable)
        return os.path.join(base, relative_path)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), relative_path)


def find_adb_executable() -> str:
    candidates = [
        get_bundled_resource_path(os.path.join("adb_tools", "adb.exe")),
        get_bundled_resource_path(os.path.join("platform-tools", "adb.exe")),
        os.path.join(os.getcwd(), "adb_tools", "adb.exe"),
        os.path.join(os.getcwd(), "platform-tools", "adb.exe"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p

    from shutil import which

    resolved = which("adb")
    return resolved or "adb"


DEFAULT_ADB_PATH = find_adb_executable()
CURRENT_ADB_PATH = DEFAULT_ADB_PATH


def set_custom_adb_path(path: str) -> None:
    global CURRENT_ADB_PATH
    if path and os.path.isfile(path):
        CURRENT_ADB_PATH = path
    else:
        CURRENT_ADB_PATH = DEFAULT_ADB_PATH


def get_woa_debug_dir() -> str:
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "woa_debug")
    os.makedirs(p, exist_ok=True)
    return p


def woa_debug_set_runtime_started() -> None:
    # 兼容旧接口，当前版本不需要额外处理。
    return


def save_image_safe(path: str, image: np.ndarray) -> bool:
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        ext = os.path.splitext(path)[1] or ".png"
        ok, buf = cv2.imencode(ext, image)
        if not ok:
            return False
        with open(path, "wb") as f:
            f.write(buf.tobytes())
        return True
    except Exception:
        return False


def read_image_safe(path: str) -> Optional[np.ndarray]:
    try:
        raw = np.fromfile(path, dtype=np.uint8)
        if raw.size == 0:
            return None
        return cv2.imdecode(raw, cv2.IMREAD_COLOR)
    except Exception:
        return None


def _run_command(cmd: Sequence[str], timeout: float = _DEFAULT_TIMEOUT) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(
            list(cmd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        out = proc.stdout.decode("utf-8", errors="ignore").strip()
        err = proc.stderr.decode("utf-8", errors="ignore").strip()
        return proc.returncode, out, err
    except FileNotFoundError:
        return 127, "", f"command not found: {cmd[0] if cmd else ''}"
    except Exception as exc:
        return 1, "", str(exc)


def probe_adb_version(adb_path: Optional[str] = None) -> Tuple[bool, str]:
    adb = adb_path or CURRENT_ADB_PATH or DEFAULT_ADB_PATH
    code, out, err = _run_command([adb, "version"], timeout=3.0)
    if code == 0:
        return True, out or "adb ok"
    return False, err or out or "adb unavailable"


def kill_adb_server() -> None:
    adb = CURRENT_ADB_PATH or DEFAULT_ADB_PATH
    try:
        _run_command([adb, "kill-server"], timeout=3.0)
    except Exception:
        pass


def close_all_and_kill_server() -> None:
    with _OPEN_LOCK:
        alive = list(_OPEN_CONTROLLERS)
    for ctrl in alive:
        try:
            ctrl.close()
        except Exception:
            pass
    kill_adb_server()


class AdbController:
    VALID_CONTROL_METHODS = ("adb",)

    def __init__(
        self,
        target_device: Optional[str] = None,
        use_minitouch: bool = False,
        screenshot_method: str = "adb",
        control_method: Optional[str] = None,
        instance_id: int = 1,
    ) -> None:
        self.instance_id = instance_id
        self.device_serial = target_device
        self.adb_path = CURRENT_ADB_PATH or DEFAULT_ADB_PATH
        self.control_method = "adb"
        self.screenshot_method = "adb"
        self.use_minitouch = use_minitouch
        self._last_ok_ts = 0.0
        self._lock = threading.Lock()

        with _OPEN_LOCK:
            _OPEN_CONTROLLERS.append(self)

    @staticmethod
    def list_devices(adb_path: Optional[str] = None) -> List[str]:
        adb = adb_path or CURRENT_ADB_PATH or DEFAULT_ADB_PATH
        code, out, _ = _run_command([adb, "devices"], timeout=5.0)
        if code != 0:
            return []
        serials: List[str] = []
        for line in out.splitlines()[1:]:
            text = line.strip()
            if not text:
                continue
            parts = text.split()
            if len(parts) >= 2 and parts[1] == "device":
                serials.append(parts[0])
        return serials

    def _base_cmd(self) -> List[str]:
        cmd = [self.adb_path]
        if self.device_serial:
            cmd.extend(["-s", self.device_serial])
        return cmd

    def run_adb(self, args: Sequence[str], timeout: float = _DEFAULT_TIMEOUT) -> Tuple[int, str, str]:
        return _run_command(self._base_cmd() + list(args), timeout=timeout)

    def connect(self, serial: str) -> bool:
        self.device_serial = serial
        code, _, _ = self.run_adb(["connect", serial], timeout=6.0)
        if code == 0:
            self._last_ok_ts = time.time()
            return True
        # 本地 USB 直连设备不需要 connect。
        return serial in self.list_devices(self.adb_path)

    def ensure_connected(self) -> bool:
        if not self.device_serial:
            return False
        code, out, _ = self.run_adb(["get-state"], timeout=4.0)
        if code == 0 and "device" in out:
            self._last_ok_ts = time.time()
            return True
        if ":" in self.device_serial:
            return self.connect(self.device_serial)
        return False

    def reconnect(self) -> bool:
        if not self.device_serial:
            return False
        try:
            self.run_adb(["disconnect", self.device_serial], timeout=4.0)
        except Exception:
            pass
        return self.connect(self.device_serial)

    def shell(self, command: str, timeout: float = _DEFAULT_TIMEOUT) -> Tuple[int, str, str]:
        args = ["shell"] + shlex.split(command)
        return self.run_adb(args, timeout=timeout)

    def tap(self, x: int, y: int) -> bool:
        code, _, _ = self.shell(f"input tap {int(x)} {int(y)}", timeout=4.0)
        return code == 0

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> bool:
        code, _, _ = self.shell(
            f"input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {int(duration_ms)}",
            timeout=5.0,
        )
        return code == 0

    def keyevent(self, key: int) -> bool:
        code, _, _ = self.shell(f"input keyevent {int(key)}", timeout=4.0)
        return code == 0

    def input_text(self, text: str) -> bool:
        safe = text.replace(" ", "%s")
        code, _, _ = self.shell(f"input text {safe}", timeout=4.0)
        return code == 0

    def list_packages(self, keyword: Optional[str] = None) -> List[str]:
        code, out, _ = self.shell("pm list packages", timeout=8.0)
        if code != 0:
            return []
        packages = []
        for line in out.splitlines():
            line = line.strip()
            if not line.startswith("package:"):
                continue
            pkg = line.split(":", 1)[1].strip()
            if keyword and keyword not in pkg:
                continue
            packages.append(pkg)
        return packages

    def install_apk(self, apk_path: str, replace: bool = True) -> Tuple[bool, str]:
        if not os.path.isfile(apk_path):
            return False, f"apk not found: {apk_path}"
        args = ["install"]
        if replace:
            args.append("-r")
        args.append(apk_path)
        code, out, err = self.run_adb(args, timeout=120.0)
        text = (out or err).strip()
        if code == 0 and "Success" in text:
            return True, text
        return False, text or "install failed"

    def launch_app(self, package_name: str, activity_name: Optional[str] = None) -> Tuple[bool, str]:
        if not package_name:
            return False, "package name is empty"
        if activity_name:
            target = f"{package_name}/{activity_name}"
            code, out, err = self.shell(f"am start -n {target}", timeout=8.0)
            if code == 0:
                return True, (out or "").strip()
            return False, (err or out or "am start failed").strip()

        code, out, err = self.shell(f"monkey -p {package_name} -c android.intent.category.LAUNCHER 1", timeout=8.0)
        if code == 0:
            return True, (out or "").strip()
        return False, (err or out or "monkey launch failed").strip()

    def resolve_launcher_activity(self, package_name: str) -> str:
        if not package_name:
            return ""
        code, out, _ = self.shell(f"cmd package resolve-activity --brief {package_name}", timeout=8.0)
        if code != 0 or not out:
            return ""
        lines = [line.strip() for line in out.splitlines() if line.strip()]
        if not lines:
            return ""
        target = lines[-1]
        if "/" not in target:
            return ""
        return target.split("/", 1)[1]

    def get_screenshot(self) -> Optional[np.ndarray]:
        def _decode_png_bytes(raw: bytes) -> Optional[np.ndarray]:
            if not raw:
                return None
            png_bytes = raw.replace(b"\r\n", b"\n")
            arr = np.frombuffer(png_bytes, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is None or img.size == 0:
                return None
            # 过滤纯黑/异常帧，避免后续 OCR 一直失败。
            mean_val = float(np.mean(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)))
            if mean_val < 2.0:
                return None
            return img

        # 路径1: exec-out screencap（最快）
        with self._lock:
            cmd = self._base_cmd() + ["exec-out", "screencap", "-p"]
            try:
                proc = subprocess.run(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=6.0,
                    creationflags=0x08000000 if os.name == "nt" else 0,
                )
            except Exception:
                proc = None

        if proc is not None and proc.returncode == 0:
            image = _decode_png_bytes(proc.stdout)
            if image is not None:
                self._last_ok_ts = time.time()
                return image

        # 路径2: 设备本地落盘 + pull 到本地再解码（更稳，避免字节在文本通道损坏）
        remote_file = "/sdcard/__woa_screen.png"
        local_file = os.path.join(get_woa_debug_dir(), f"screen_{int(time.time() * 1000)}.png")
        code, _, _ = self.run_adb(["shell", "screencap", "-p", remote_file], timeout=8.0)
        if code != 0:
            return None

        code, _, _ = self.run_adb(["pull", remote_file, local_file], timeout=12.0)
        self.run_adb(["shell", "rm", "-f", remote_file], timeout=4.0)
        if code != 0:
            return None

        image = read_image_safe(local_file)
        try:
            os.remove(local_file)
        except Exception:
            pass

        if image is None or image.size == 0:
            return None
        mean_val = float(np.mean(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)))
        if mean_val < 2.0:
            return None

        self._last_ok_ts = time.time()
        return image

    def close(self) -> None:
        with _OPEN_LOCK:
            if self in _OPEN_CONTROLLERS:
                _OPEN_CONTROLLERS.remove(self)


def ensure_local_platform_tools() -> Dict[str, object]:
    src = get_bundled_resource_path("platform-tools")
    dst = get_bundled_resource_path("adb_tools")
    copied: List[str] = []
    errors: List[str] = []
    os.makedirs(dst, exist_ok=True)

    if not os.path.isdir(src):
        return {
            "source_dir": src,
            "target_dir": dst,
            "copied": copied,
            "adb_path": os.path.join(dst, "adb.exe") if os.path.isfile(os.path.join(dst, "adb.exe")) else "",
            "ready": os.path.isfile(os.path.join(dst, "adb.exe")),
            "errors": ["platform-tools not found"],
        }

    for name in os.listdir(src):
        src_file = os.path.join(src, name)
        dst_file = os.path.join(dst, name)
        if not os.path.isfile(src_file):
            continue
        if os.path.isfile(dst_file):
            continue
        try:
            with open(src_file, "rb") as r, open(dst_file, "wb") as w:
                w.write(r.read())
            copied.append(name)
        except Exception as exc:
            errors.append(f"copy {name} failed: {exc}")

    adb_path = os.path.join(dst, "adb.exe")
    ready = os.path.isfile(adb_path)
    return {
        "source_dir": src,
        "target_dir": dst,
        "copied": copied,
        "adb_path": adb_path if ready else "",
        "ready": ready,
        "errors": errors,
    }


@atexit.register
def _cleanup() -> None:
    with _OPEN_LOCK:
        alive = list(_OPEN_CONTROLLERS)
    for ctrl in alive:
        try:
            ctrl.close()
        except Exception:
            pass
