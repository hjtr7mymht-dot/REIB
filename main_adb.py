from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import requests

from adb_controller import AdbController
from simple_ocr import OCRField, SimpleOCR, StopSignal


@dataclass
class AlertEvent:
    level: str
    title: str
    message: str
    metrics: Dict[str, object] = field(default_factory=dict)


@dataclass
class BotConfig:
    adb_path: str = ""
    target_device: str = ""
    interval_sec: float = 8.0
    water_temp_low: float = -5.0
    water_temp_high: float = 35.0
    battery_min: int = 20
    signal_dbm_min: int = -105
    weak_network_retry: int = 3
    api_enabled: bool = False
    app_api_url: str = ""
    app_api_method: str = "GET"
    app_api_headers: Dict[str, object] = field(default_factory=dict)
    app_api_params: Dict[str, object] = field(default_factory=dict)
    app_api_body: Dict[str, object] = field(default_factory=dict)
    app_api_timeout_sec: float = 6.0
    api_temp_key: str = "data.temperature"
    api_battery_key: str = "data.battery"
    api_signal_key: str = "data.signal_dbm"
    webhook_provider: str = "wecom"
    webhook_url: str = ""
    field_map: List[OCRField] = field(default_factory=list)

    @staticmethod
    def from_dict(data: Dict[str, object]) -> "BotConfig":
        cfg = BotConfig()
        cfg.adb_path = str(data.get("adb_path") or "")
        cfg.target_device = str(data.get("target_device") or "")
        cfg.interval_sec = float(data.get("interval_sec") or 8.0)
        cfg.water_temp_low = float(data.get("water_temp_low") or -5.0)
        cfg.water_temp_high = float(data.get("water_temp_high") or 35.0)
        cfg.battery_min = int(data.get("battery_min") or 20)
        cfg.signal_dbm_min = int(data.get("signal_dbm_min") or -105)
        cfg.weak_network_retry = int(data.get("weak_network_retry") or 3)
        cfg.api_enabled = bool(data.get("api_enabled") or False)
        cfg.app_api_url = str(data.get("app_api_url") or "")
        cfg.app_api_method = str(data.get("app_api_method") or "GET").upper()
        cfg.app_api_timeout_sec = float(data.get("app_api_timeout_sec") or 6.0)
        cfg.api_temp_key = str(data.get("api_temp_key") or "data.temperature")
        cfg.api_battery_key = str(data.get("api_battery_key") or "data.battery")
        cfg.api_signal_key = str(data.get("api_signal_key") or "data.signal_dbm")
        cfg.webhook_provider = str(data.get("webhook_provider") or "wecom")
        cfg.webhook_url = str(data.get("webhook_url") or data.get("mobile_notify_webhook") or "")

        headers_raw = data.get("app_api_headers")
        if isinstance(headers_raw, dict):
            cfg.app_api_headers = headers_raw
        elif isinstance(headers_raw, str) and headers_raw.strip():
            try:
                parsed = json.loads(headers_raw)
                if isinstance(parsed, dict):
                    cfg.app_api_headers = parsed
            except Exception:
                pass

        params_raw = data.get("app_api_params")
        if isinstance(params_raw, dict):
            cfg.app_api_params = params_raw
        elif isinstance(params_raw, str) and params_raw.strip():
            try:
                parsed = json.loads(params_raw)
                if isinstance(parsed, dict):
                    cfg.app_api_params = parsed
            except Exception:
                pass

        body_raw = data.get("app_api_body")
        if isinstance(body_raw, dict):
            cfg.app_api_body = body_raw
        elif isinstance(body_raw, str) and body_raw.strip():
            try:
                parsed = json.loads(body_raw)
                if isinstance(parsed, dict):
                    cfg.app_api_body = parsed
            except Exception:
                pass

        parsed_fields: List[OCRField] = []
        fields_raw = data.get("field_map")
        if isinstance(fields_raw, list):
            for item in fields_raw:
                if not isinstance(item, dict):
                    continue
                region = item.get("region")
                if not isinstance(region, list) or len(region) != 4:
                    continue
                try:
                    parsed_fields.append(
                        OCRField(
                            name=str(item.get("name") or ""),
                            region=(int(region[0]), int(region[1]), int(region[2]), int(region[3])),
                            pattern=str(item.get("pattern") or ""),
                        )
                    )
                except Exception:
                    continue
        cfg.field_map = parsed_fields
        return cfg


class WoaBot:
    def __init__(
        self,
        log_callback: Optional[Callable[[str], None]] = None,
        config_callback: Optional[Callable[[], Dict[str, object]]] = None,
        metrics_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        alert_callback: Optional[Callable[[AlertEvent], None]] = None,
        instance_id: int = 1,
    ) -> None:
        self.instance_id = instance_id
        self.log_callback = log_callback
        self.config_callback = config_callback
        self.metrics_callback = metrics_callback
        self.alert_callback = alert_callback

        self.running = False
        self._worker_thread: Optional[threading.Thread] = None

        self.adb: Optional[AdbController] = None
        self.ocr: Optional[SimpleOCR] = None
        self.config = BotConfig()

        self._last_alert_ts: Dict[str, float] = {}
        self._alert_cooldown_sec = 180.0
        self._task_completed = 0
        self._task_total = 12
        self._healthy_rounds = 0

    def log(self, message: str) -> None:
        text = message.strip()
        if not text:
            return
        if self.log_callback:
            self.log_callback(text)
        else:
            print(text)

    def _emit_metrics(self, payload: Dict[str, Any]) -> None:
        if self.metrics_callback:
            try:
                self.metrics_callback(payload)
            except Exception:
                pass

    def _emit_alert_event(self, event: AlertEvent) -> None:
        if self.alert_callback:
            try:
                self.alert_callback(event)
            except Exception:
                pass

    def _load_runtime_config(self) -> BotConfig:
        raw: Dict[str, object] = {}
        if self.config_callback:
            raw = self.config_callback() or {}
        self.config = BotConfig.from_dict(raw)
        return self.config

    def _build_controller(self) -> None:
        self.adb = AdbController(target_device=self.config.target_device, instance_id=self.instance_id)
        if self.config.adb_path:
            self.adb.adb_path = self.config.adb_path
        self.ocr = SimpleOCR(adb_controller=self.adb)

    def _notify_webhook(self, event: AlertEvent) -> None:
        if not self.config.webhook_url:
            return
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "content": (
                    f"### {event.title}\n"
                    f"> 等级: **{event.level.upper()}**\n\n"
                    f"{event.message}\n\n"
                    f"指标: `{json.dumps(event.metrics, ensure_ascii=False)}`"
                )
            },
        }
        last_exc: Optional[Exception] = None
        for _ in range(2):
            try:
                resp = requests.post(self.config.webhook_url, json=payload, timeout=6)
                resp.raise_for_status()
                return
            except Exception as exc:
                last_exc = exc
        if last_exc is not None:
            self.log(f"[告警] 推送失败: {last_exc}")

    def _emit_alert(self, key: str, event: AlertEvent) -> None:
        now = time.time()
        if now - self._last_alert_ts.get(key, 0.0) < self._alert_cooldown_sec:
            return
        self._last_alert_ts[key] = now
        self.log(f"[告警] {event.title} - {event.message}")
        self._emit_alert_event(event)
        self._notify_webhook(event)

    def _evaluate_metrics(self, metrics: Dict[str, object]) -> None:
        temp = metrics.get("temperature")
        battery = metrics.get("battery")
        signal = metrics.get("signal_dbm")

        if isinstance(temp, (int, float)):
            if temp < self.config.water_temp_low:
                self._emit_alert(
                    "temp_low",
                    AlertEvent("warning", "饮水温度过低", f"检测值 {temp}℃，低于阈值 {self.config.water_temp_low}℃", metrics),
                )
            if temp > self.config.water_temp_high:
                self._emit_alert(
                    "temp_high",
                    AlertEvent("warning", "饮水温度过高", f"检测值 {temp}℃，高于阈值 {self.config.water_temp_high}℃", metrics),
                )

        if isinstance(battery, int) and battery < self.config.battery_min:
            self._emit_alert(
                "battery_low",
                AlertEvent("critical", "终端电量不足", f"当前电量 {battery}%，低于阈值 {self.config.battery_min}%", metrics),
            )

        if isinstance(signal, int) and signal < self.config.signal_dbm_min:
            self._emit_alert(
                "signal_weak",
                AlertEvent("warning", "网络信号较弱", f"当前信号 {signal}dBm，低于阈值 {self.config.signal_dbm_min}dBm", metrics),
            )

    def _publish_snapshot(self, connected: bool, metrics: Optional[Dict[str, object]] = None, note: str = "") -> None:
        payload: Dict[str, Any] = {
            "connected": connected,
            "device": self.config.target_device,
            "metrics": metrics or {},
            "task_completed": self._task_completed,
            "task_total": self._task_total,
            "healthy_rounds": self._healthy_rounds,
            "note": note,
            "timestamp": time.time(),
        }
        self._emit_metrics(payload)

    def _get_by_path(self, payload: Dict[str, object], path: str) -> object:
        current: object = payload
        if not path:
            return None
        for part in path.split("."):
            key = part.strip()
            if not key:
                continue
            if isinstance(current, dict) and key in current:
                current = current.get(key)
            else:
                return None
        return current

    def _as_float(self, value: object) -> Optional[float]:
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            text = value.strip().replace("%", "")
            try:
                return float(text)
            except Exception:
                return None
        return None

    def _as_int(self, value: object) -> Optional[int]:
        f = self._as_float(value)
        if f is None:
            return None
        return int(round(f))

    def _read_metrics_from_api(self) -> Optional[Dict[str, object]]:
        cfg = self.config
        if not cfg.api_enabled or not cfg.app_api_url:
            return None

        method = cfg.app_api_method if cfg.app_api_method in ("GET", "POST") else "GET"
        try:
            if method == "GET":
                resp = requests.get(
                    cfg.app_api_url,
                    headers=cfg.app_api_headers or None,
                    params=cfg.app_api_params or None,
                    timeout=max(2.0, cfg.app_api_timeout_sec),
                )
            else:
                resp = requests.post(
                    cfg.app_api_url,
                    headers=cfg.app_api_headers or None,
                    params=cfg.app_api_params or None,
                    json=cfg.app_api_body or None,
                    timeout=max(2.0, cfg.app_api_timeout_sec),
                )
            resp.raise_for_status()
            payload = resp.json()
            if not isinstance(payload, dict):
                return None

            temp = self._as_float(self._get_by_path(payload, cfg.api_temp_key))
            battery = self._as_int(self._get_by_path(payload, cfg.api_battery_key))
            signal = self._as_int(self._get_by_path(payload, cfg.api_signal_key))

            return {
                "temperature": temp,
                "battery": battery,
                "signal_dbm": signal,
                "raw_text": json.dumps(payload, ensure_ascii=False)[:800],
                "source": "api",
            }
        except Exception as exc:
            self.log(f"[API] 读取失败: {exc}")
            return None

    def _poll_once(self) -> None:
        if not self.adb or not self.ocr:
            raise RuntimeError("Runtime not initialized")

        # API 直连优先，失败时自动回退 OCR。
        api_metrics = self._read_metrics_from_api()
        if api_metrics is not None:
            self._healthy_rounds += 1
            self._task_completed = min(self._task_total, self._task_completed + 1)
            self.log(f"[巡检] API指标: {json.dumps(api_metrics, ensure_ascii=False)}")
            self._publish_snapshot(True, metrics=api_metrics, note="API直连读取成功")
            self._evaluate_metrics(api_metrics)
            return

        if not self.adb.ensure_connected():
            self._publish_snapshot(False, note="设备连接失败")
            self._emit_alert("disconnect", AlertEvent("critical", "边缘节点失联", f"设备 {self.config.target_device or '未指定'} 无法连接"))
            return

        screen = self.adb.get_screenshot()
        if screen is None:
            self._publish_snapshot(True, note="截图失败")
            self._emit_alert("screenshot_fail", AlertEvent("warning", "截图失败", "未获取到设备截图"))
            return

        metrics = self.ocr.extract_metrics(screen=screen, field_map=self.config.field_map)
        task_progress = metrics.get("task_progress")
        if isinstance(task_progress, str) and "/" in task_progress:
            try:
                done_text, total_text = task_progress.split("/", 1)
                done = int(done_text)
                total = max(1, int(total_text))
                self._task_total = total
                self._task_completed = max(0, min(done, total))
            except Exception:
                self._task_completed = min(self._task_total, self._task_completed + 1)
        else:
            self._task_completed = min(self._task_total, self._task_completed + 1)
        self._healthy_rounds += 1
        self.log(f"[巡检] 指标: {json.dumps(metrics, ensure_ascii=False)}")
        self._publish_snapshot(True, metrics=metrics, note="巡检成功")
        self._evaluate_metrics(metrics)

    def _loop(self) -> None:
        retry_budget = max(1, self.config.weak_network_retry)
        retry_count = 0

        while self.running:
            try:
                self._poll_once()
                retry_count = 0
            except StopSignal:
                break
            except Exception as exc:
                retry_count += 1
                self.log(f"[系统] 本轮巡检异常: {exc}")
                if retry_count >= retry_budget:
                    self._emit_alert(
                        "loop_error",
                        AlertEvent("critical", "巡检循环异常", f"连续失败 {retry_count} 次，系统继续重试"),
                    )
                    retry_count = 0

            wait_until = time.time() + max(1.0, self.config.interval_sec)
            while self.running and time.time() < wait_until:
                time.sleep(0.2)

        self.log("[系统] 巡检循环已停止")

    def start(self) -> None:
        if self.running:
            self.log("[系统] 巡检已在运行")
            return

        cfg = self._load_runtime_config()
        self._build_controller()
        self.running = True
        self._last_alert_ts.clear()
        self._task_completed = 0
        self._healthy_rounds = 0
        self.log(
            f"[系统] 启动成功: device={cfg.target_device or '未选择'}, interval={cfg.interval_sec}s, "
            f"threshold(temp={cfg.water_temp_low}~{cfg.water_temp_high}, battery>={cfg.battery_min}%)"
        )
        self._publish_snapshot(False, note="巡检任务启动")

        self._worker_thread = threading.Thread(target=self._loop, name=f"RanchBot-{self.instance_id}", daemon=True)
        self._worker_thread.start()

    def stop(self) -> None:
        if not self.running:
            return
        self.running = False
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=2.0)
        self._worker_thread = None
        if self.adb:
            self.adb.close()
        self._publish_snapshot(False, note="巡检任务已停止")
        self.log("[系统] 巡检任务已停止")


def load_config_from_file(path: str) -> Dict[str, object]:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def save_config_to_file(path: str, data: Dict[str, object]) -> bool:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False
