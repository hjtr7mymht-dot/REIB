from __future__ import annotations

import datetime
import copy
import json
import os
import queue
import random
import tkinter as tk
import requests
from urllib.parse import parse_qs, urlparse
from tkinter import TclError, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText
from typing import Any, Dict, List
from tkinter import ttk

import ttkbootstrap as ttkb  # type: ignore[import-untyped]
from ttkbootstrap.constants import BOTH, END, LEFT, RIGHT, X

from adb_controller import CURRENT_ADB_PATH, AdbController, probe_adb_version, set_custom_adb_path
from emulator_discovery import discover_device_entries, get_mumu_adb_paths, select_working_adb_path
from apk_interface_reader import analyze_apk_interfaces
from main_adb import AlertEvent, WoaBot, load_config_from_file, save_config_to_file
from simple_ocr import SimpleOCR


_INSTANCE_ID = 1
CONFIG_FILE = "config.json" if _INSTANCE_ID == 1 else f"config_{_INSTANCE_ID}.json"


class App:
    def __init__(self) -> None:
        self.root = ttkb.Window(themename="minty")
        self.root.title("智巡草原 · 牧区无人巡检系统")
        self.root.geometry("1280x860")
        self.root.minsize(1120, 760)

        self.log_queue: queue.Queue[str] = queue.Queue()
        self.ui_queue: queue.Queue[tuple[str, Dict[str, Any]]] = queue.Queue()

        self.vars = {
            "adb_path": tk.StringVar(value=CURRENT_ADB_PATH),
            "target_device": tk.StringVar(value=""),
            "interval_sec": tk.StringVar(value="8"),
            "water_temp_low": tk.StringVar(value="-5"),
            "water_temp_high": tk.StringVar(value="35"),
            "battery_min": tk.StringVar(value="20"),
            "signal_dbm_min": tk.StringVar(value="-105"),
            "weak_network_retry": tk.StringVar(value="3"),
            "api_enabled": tk.BooleanVar(value=False),
            "app_api_url": tk.StringVar(value=""),
            "app_api_method": tk.StringVar(value="GET"),
            "app_api_headers": tk.StringVar(value="{}"),
            "app_api_params": tk.StringVar(value="{}"),
            "app_api_body": tk.StringVar(value="{}"),
            "api_temp_key": tk.StringVar(value="data.temperature"),
            "api_battery_key": tk.StringVar(value="data.battery"),
            "api_signal_key": tk.StringVar(value="data.signal_dbm"),
            "webhook_url": tk.StringVar(value=""),
            "webhook_provider": tk.StringVar(value="wecom"),
            "apk_path": tk.StringVar(value=""),
            "app_package": tk.StringVar(value=""),
            "app_activity": tk.StringVar(value=""),
            "debug_always_on": tk.BooleanVar(value=False),
        }
        self.device_display_var = tk.StringVar(value="")

        self.latest_payload: Dict[str, Any] = {}
        self.alert_blocks: List[str] = []
        self.discovered_devices: List[Dict[str, Any]] = []
        self.debug_mode_enabled = False
        self.debug_payload: Dict[str, Any] = {}
        self._is_closing = False

        self.bot = WoaBot(
            log_callback=self.enqueue_log,
            config_callback=self.collect_config,
            metrics_callback=self.on_metrics,
            alert_callback=self.on_alert,
            instance_id=_INSTANCE_ID,
        )

        self._build_ui()
        self.load_config()
        self.seed_demo()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(120, self.flush_queues)

    @staticmethod
    def _is_probable_launch_activity(name: str) -> bool:
        if not name or "." not in name:
            return False
        lower = name.lower()
        if any(k in lower for k in ("permission", "provider", "receiver", "service")):
            return False
        return lower.endswith("activity") or ".activity" in lower or "mainactivity" in lower

    def _build_ui(self) -> None:
        shell = ttkb.Frame(self.root, padding=(10, 8, 10, 8))
        shell.pack(fill=BOTH, expand=True)

        header = ttkb.Frame(shell, padding=(4, 2, 4, 8))
        header.pack(fill=X)
        left = ttkb.Frame(header)
        left.pack(side=LEFT, fill=X, expand=True)
        ttkb.Label(left, text="智巡草原 · 牧区无人巡检", font=("Microsoft YaHei UI", 23, "bold")).pack(anchor="w")
        ttkb.Label(left, text="边缘计算 · 异构设备控制 · 弱网自愈", bootstyle="secondary").pack(anchor="w")

        right = ttkb.Frame(header)
        right.pack(side=RIGHT)
        ttkb.Button(right, text="刷新数据", bootstyle="success", command=self.refresh_dashboard).pack(side=LEFT, padx=4)
        ttkb.Button(right, text="模拟巡检", bootstyle="secondary", command=self.simulate_inspection).pack(side=LEFT, padx=4)
        ttkb.Button(right, text="模拟告警", bootstyle="warning", command=self.simulate_alert).pack(side=LEFT, padx=4)
        self.cluster_status = ttkb.Label(right, text="集群在线 · 0节点", bootstyle="inverse-success", padding=(10, 4))
        self.cluster_status.pack(side=LEFT, padx=(10, 0))

        card_wrap = ttkb.Frame(shell)
        card_wrap.pack(fill=X, pady=(0, 8))
        for i in range(4):
            card_wrap.grid_columnconfigure(i, weight=1)

        self.temp_card = self._make_card(card_wrap, "饮水槽监测", "--.-°C", "+0.0")
        self.ear_card = self._make_card(card_wrap, "耳标在线率", "--%", "+0.0")
        self.battery_card = self._make_card(card_wrap, "终端健康度", "--%", "+0.0")
        self.task_card = self._make_card(card_wrap, "巡检任务", "0/12", "0%")
        self.coverage_card = self._make_card(card_wrap, "巡检覆盖率", "--%", "+0.0")
        self.alert_rate_card = self._make_card(card_wrap, "告警检出率", "--%", "-0.0")
        self.ocr_acc_card = self._make_card(card_wrap, "OCR准确率", "--%", "+0.0")
        self.rsp_card = self._make_card(card_wrap, "平均响应", "--%", "+0.0")

        top_cards = [self.temp_card, self.ear_card, self.battery_card, self.task_card]
        bot_cards = [self.coverage_card, self.alert_rate_card, self.ocr_acc_card, self.rsp_card]
        for i, card in enumerate(top_cards):
            card["frame"].grid(row=0, column=i, padx=6, pady=6, sticky="nsew")
        for i, card in enumerate(bot_cards):
            card["frame"].grid(row=1, column=i, padx=6, pady=6, sticky="nsew")

        body = ttkb.Frame(shell)
        body.pack(fill=BOTH, expand=True)
        body.grid_columnconfigure(0, weight=3)
        body.grid_columnconfigure(1, weight=2)
        body.grid_rowconfigure(0, weight=1)

        left_panel = ttkb.Labelframe(body, text="异构设备控制 · ADB 集群", padding=12)
        left_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 8))

        quick = ttkb.Frame(left_panel)
        quick.pack(fill=X, pady=(0, 8))
        ttkb.Label(quick, text="目标设备").pack(side=LEFT, padx=(2, 6))
        self.device_combo = ttk.Combobox(quick, textvariable=self.device_display_var, width=28, state="readonly")
        self.device_combo.pack(side=LEFT, padx=4)
        self.device_combo.bind("<<ComboboxSelected>>", self._on_device_selected)
        ttkb.Button(quick, text="扫描并连接", command=self.scan_devices).pack(side=LEFT, padx=4)
        ttkb.Button(quick, text="连接所选设备", bootstyle="secondary", command=self.connect_selected_device).pack(side=LEFT, padx=4)
        ttkb.Button(quick, text="ADB诊断", bootstyle="primary", command=self.adb_diagnose).pack(side=LEFT, padx=4)
        ttkb.Button(quick, text="一键自检", bootstyle="info", command=self.run_system_self_check).pack(side=LEFT, padx=4)

        nodes = ttkb.Frame(left_panel)
        nodes.pack(fill=X, pady=(0, 8))
        self.node_lines: Dict[str, ttkb.Label] = {}
        for name in ("牧场-A区", "牧场-B区", "牧场-C区"):
            row = ttkb.Frame(nodes, padding=(8, 6))
            row.pack(fill=X, pady=2)
            tag = ttkb.Label(row, text=name, font=("Microsoft YaHei UI", 12, "bold"))
            tag.pack(side=LEFT)
            stat = ttkb.Label(row, text="离线", bootstyle="danger")
            stat.pack(side=RIGHT)
            self.node_lines[name] = stat

        ocr_block = ttkb.Frame(left_panel)
        ocr_block.pack(fill=X, pady=(4, 8))
        ttkb.Label(ocr_block, text="最新OCR感知", font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
        self.ocr_progress = ttkb.Progressbar(ocr_block, bootstyle="success-striped", mode="determinate", value=0, maximum=100)
        self.ocr_progress.pack(fill=X, pady=(6, 6))
        self.ocr_text = ScrolledText(ocr_block, height=4, wrap="word")
        self.ocr_text.pack(fill=X)

        cfg = ttkb.Labelframe(left_panel, text="控制与接入", padding=8)
        cfg.pack(fill=X, pady=(2, 6))
        ttkb.Label(cfg, text="ADB 路径").grid(row=0, column=0, sticky="w", padx=4)
        ttkb.Entry(cfg, textvariable=self.vars["adb_path"], width=52).grid(row=0, column=1, sticky="we", padx=4)
        ttkb.Button(cfg, text="自动发现", command=self.auto_fill_adb).grid(row=0, column=2, padx=4)

        ttkb.Label(cfg, text="APK 文件").grid(row=1, column=0, sticky="w", padx=4)
        ttkb.Entry(cfg, textvariable=self.vars["apk_path"], width=52).grid(row=1, column=1, sticky="we", padx=4)
        ttkb.Button(cfg, text="选择APK", command=self.choose_apk_file).grid(row=1, column=2, padx=4)

        ttkb.Label(cfg, text="包名").grid(row=2, column=0, sticky="w", padx=4)
        ttkb.Entry(cfg, textvariable=self.vars["app_package"], width=24).grid(row=2, column=1, sticky="w", padx=4)
        ttkb.Label(cfg, text="入口").grid(row=2, column=1, sticky="e", padx=(0, 160))
        ttkb.Entry(cfg, textvariable=self.vars["app_activity"], width=24).grid(row=2, column=1, sticky="e", padx=4)

        btns = ttkb.Frame(cfg)
        btns.grid(row=3, column=0, columnspan=3, sticky="w", padx=4, pady=6)
        ttkb.Button(btns, text="保存配置", bootstyle="info", command=self.save_config).pack(side=LEFT, padx=3)
        ttkb.Button(btns, text="安装并启动App", bootstyle="primary", command=self.install_and_launch_app).pack(side=LEFT, padx=3)
        ttkb.Button(btns, text="启动巡检", bootstyle="success", command=self.start_bot).pack(side=LEFT, padx=3)
        ttkb.Button(btns, text="停止巡检", bootstyle="danger", command=self.stop_bot).pack(side=LEFT, padx=3)
        ttkb.Button(btns, text="高级设置", bootstyle="secondary", command=self.open_advanced_settings).pack(side=LEFT, padx=3)

        ttkb.Label(left_panel, text="运行日志", bootstyle="secondary").pack(anchor="w", pady=(4, 2))
        self.log_text = ScrolledText(left_panel, height=7, wrap="word")
        self.log_text.pack(fill=BOTH, expand=True)

        ttkb.Label(left_panel, text="设备细节", bootstyle="secondary").pack(anchor="w", pady=(4, 2))
        self.device_text = ScrolledText(left_panel, height=4, wrap="word")
        self.device_text.pack(fill=X)

        right_panel = ttkb.Labelframe(body, text="实时告警 · 钉钉/企微同步", padding=12)
        right_panel.grid(row=0, column=1, sticky="nsew")
        self.alert_text = ScrolledText(right_panel, height=24, wrap="word")
        self.alert_text.pack(fill=BOTH, expand=True)
        self.alert_text.tag_config("critical", foreground="#B42318")
        self.alert_text.tag_config("warning", foreground="#B54708")
        self.alert_text.tag_config("info", foreground="#175CD3")

        bottom = ttkb.Frame(right_panel)
        bottom.pack(fill=X, pady=(10, 0))
        ttkb.Button(bottom, text="模拟告警", bootstyle="warning-outline", command=self.simulate_alert).pack(side=RIGHT)
        ttkb.Label(bottom, text="企业微信 / 钉钉 已接入", bootstyle="secondary").pack(side=LEFT)

    def _make_card(self, parent, title: str, value: str, trend: str) -> Dict[str, Any]:
        icon_map = {
            "饮水槽监测": "💧",
            "耳标在线率": "🐄",
            "终端健康度": "🧭",
            "巡检任务": "🤖",
            "巡检覆盖率": "📊",
            "告警检出率": "🎯",
            "OCR准确率": "📸",
            "平均响应": "⏱",
        }
        frame = ttkb.Frame(parent, padding=14, bootstyle="light")
        ttkb.Label(
            frame,
            text=f"{icon_map.get(title, '•')} {title}",
            font=("Microsoft YaHei UI", 10, "bold"),
            bootstyle="secondary",
        ).pack(anchor="w")
        value_label = ttkb.Label(frame, text=value, font=("Microsoft YaHei UI", 30, "bold"))
        value_label.pack(anchor="w")
        trend_label = ttkb.Label(frame, text=trend, font=("Microsoft YaHei UI", 12), bootstyle="success")
        trend_label.pack(anchor="w")
        return {"frame": frame, "value": value_label, "trend": trend_label}

    def seed_demo(self) -> None:
        self.ocr_text.delete("1.0", END)
        self.ocr_text.insert(
            END,
            "> [初始化] 等待首轮巡检\n"
            "> 温度: -- | 电量: -- | 信号: --\n"
            "> ADB保活待命\n",
        )
        self._render_devices("未指定", False, "等待扫描设备")
        self._push_alert("warning", "系统启动", "控制台已启动，等待设备连接")

    def enqueue_log(self, text: str) -> None:
        if self._is_closing:
            return
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self.log_queue.put(f"[{ts}] {text}")

    def on_metrics(self, payload: Dict[str, Any]) -> None:
        if self._is_closing:
            return
        payload = self._resolve_runtime_payload(payload)
        self.ui_queue.put(("metrics", payload))

    def on_alert(self, event: AlertEvent) -> None:
        if self._is_closing:
            return
        self.ui_queue.put(("alert", {"level": event.level, "title": event.title, "message": event.message}))

    def flush_queues(self) -> None:
        if self._is_closing:
            return
        while True:
            try:
                line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            try:
                self.log_text.insert(END, line + "\n")
                self.log_text.see(END)
            except TclError:
                return

        while True:
            try:
                typ, payload = self.ui_queue.get_nowait()
            except queue.Empty:
                break

            if typ == "metrics":
                self.apply_metrics(payload)
            elif typ == "alert":
                self._push_alert(str(payload.get("level", "warning")), str(payload.get("title", "未知告警")), str(payload.get("message", "")))

        if not self._is_closing:
            try:
                self.root.after(120, self.flush_queues)
            except TclError:
                return

    def apply_metrics(self, payload: Dict[str, Any]) -> None:
        self.latest_payload = payload
        metrics = payload.get("metrics", {}) or {}

        temp = metrics.get("temperature")
        battery = metrics.get("battery")
        signal = metrics.get("signal_dbm")
        ear_online_rate = metrics.get("ear_online_rate")
        task_progress = metrics.get("task_progress")

        temp_text = f"{float(temp):.1f}°C" if isinstance(temp, (int, float)) else "--.-°C"
        battery_text = f"{int(battery)}%" if isinstance(battery, int) else "--%"
        signal_text = f"信号 {int(signal)}dBm" if isinstance(signal, int) else "信号 --"

        ear_rate = int(ear_online_rate) if isinstance(ear_online_rate, (int, float)) else None
        if isinstance(task_progress, str) and "/" in task_progress:
            try:
                task_done = int(task_progress.split("/", 1)[0])
                task_total = int(task_progress.split("/", 1)[1])
            except Exception:
                task_done = int(payload.get("task_completed", 0))
                task_total = int(payload.get("task_total", 12))
        else:
            task_done = int(payload.get("task_completed", 0))
            task_total = int(payload.get("task_total", 12))

        self.temp_card["value"].configure(text=temp_text)
        self.temp_card["trend"].configure(text="--")
        self.ear_card["value"].configure(text=f"{ear_rate}%" if ear_rate is not None else "--%")
        self.ear_card["trend"].configure(text="--")
        self.battery_card["value"].configure(text=battery_text)
        self.battery_card["trend"].configure(text="--")
        self.task_card["value"].configure(text=f"{task_done}/{task_total}")
        percent = int((task_done / max(1, task_total)) * 100)
        self.task_card["trend"].configure(text=f"{percent}%")

        coverage = metrics.get("coverage_rate")
        if isinstance(coverage, (int, float)):
            coverage_text = f"{int(max(0, min(100, coverage)))}%"
        else:
            coverage_text = f"{percent}%"

        alert_rate = metrics.get("alert_rate")
        alert_rate_text = f"{int(max(0, min(100, float(alert_rate))))}%" if isinstance(alert_rate, (int, float)) else "--%"

        ocr_acc = metrics.get("ocr_accuracy")
        ocr_acc_text = f"{int(max(0, min(100, float(ocr_acc))))}%" if isinstance(ocr_acc, (int, float)) else "--%"
        ocr_acc_val = int(max(0, min(100, float(ocr_acc)))) if isinstance(ocr_acc, (int, float)) else 0

        avg_rsp = metrics.get("avg_response")
        avg_rsp_text = f"{int(max(0, min(100, float(avg_rsp))))}%" if isinstance(avg_rsp, (int, float)) else "--%"

        self.coverage_card["value"].configure(text=coverage_text)
        self.coverage_card["trend"].configure(text="--")
        self.alert_rate_card["value"].configure(text=alert_rate_text)
        self.alert_rate_card["trend"].configure(text="--")
        self.ocr_acc_card["value"].configure(text=ocr_acc_text)
        self.ocr_acc_card["trend"].configure(text="--")
        self.rsp_card["value"].configure(text=avg_rsp_text)
        self.rsp_card["trend"].configure(text="--")
        self.ocr_progress.configure(value=ocr_acc_val)

        connected = bool(payload.get("connected", False))
        self.cluster_status.configure(
            text="集群在线 · 3节点运行中" if connected else "集群异常 · 节点重连中",
            bootstyle="inverse-success" if connected else "inverse-warning",
        )

        device = str(payload.get("device") or "未指定")
        note = str(payload.get("note") or "")
        self._render_devices(device, connected, note)

        self.ocr_text.delete("1.0", END)
        self.ocr_text.insert(
            END,
            f"> [{datetime.datetime.now().strftime('%H:%M:%S')}] 巡检识别\n"
            f"> 温度: {temp_text} | 电量: {battery_text} | {signal_text}\n"
            f"> 耳标在线率: {str(ear_rate) + '%' if ear_rate is not None else '--'} | 任务: {task_done}/{task_total}\n"
            f"> 备注: {note}\n",
        )

    def _render_devices(self, device: str, connected: bool, note: str) -> None:
        online_style = "success" if connected else "danger"
        if hasattr(self, "node_lines"):
            if "牧场-A区" in self.node_lines:
                self.node_lines["牧场-A区"].configure(text="在线" if connected else "离线", bootstyle=online_style)
            if "牧场-B区" in self.node_lines:
                self.node_lines["牧场-B区"].configure(text="在线" if connected else "弱网", bootstyle="warning" if not connected else "success")
            if "牧场-C区" in self.node_lines:
                self.node_lines["牧场-C区"].configure(text="在线", bootstyle="success")

        lines = [
            "[设备状态]",
            f"1) 主巡检节点 {device} · {'在线' if connected else '离线/重连'}",
            f"2) 耳标阅读器从节点 · {'在线' if connected else '弱网'}",
            "3) 气象站工控屏 · 弱网自愈监控中",
            f"备注: {note}",
        ]
        self.device_text.delete("1.0", END)
        self.device_text.insert(END, "\n".join(lines))

    def _push_alert(self, level: str, title: str, message: str) -> None:
        prefix = "[CRITICAL]" if level.lower() == "critical" else "[WARNING]"
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        block = f"{prefix} {title}\n{message}\n时间: {ts} · 已推送至企业微信/钉钉\n" + ("-" * 52) + "\n"
        self.alert_blocks.insert(0, block)
        self.alert_blocks = self.alert_blocks[:8]

        self.alert_text.delete("1.0", END)
        for item in self.alert_blocks:
            tag = "critical" if "[CRITICAL]" in item else "warning"
            self.alert_text.insert(END, item, tag)

    def collect_config(self) -> Dict[str, Any]:
        return {
            "adb_path": self.vars["adb_path"].get().strip(),
            "target_device": self.vars["target_device"].get().strip(),
            "interval_sec": float(self.vars["interval_sec"].get().strip() or 8),
            "water_temp_low": float(self.vars["water_temp_low"].get().strip() or -5),
            "water_temp_high": float(self.vars["water_temp_high"].get().strip() or 35),
            "battery_min": int(self.vars["battery_min"].get().strip() or 20),
            "signal_dbm_min": int(self.vars["signal_dbm_min"].get().strip() or -105),
            "weak_network_retry": int(self.vars["weak_network_retry"].get().strip() or 3),
            "api_enabled": bool(self.vars["api_enabled"].get()),
            "app_api_url": self.vars["app_api_url"].get().strip(),
            "app_api_method": self.vars["app_api_method"].get().strip() or "GET",
            "app_api_headers": self.vars["app_api_headers"].get().strip() or "{}",
            "app_api_params": self.vars["app_api_params"].get().strip() or "{}",
            "app_api_body": self.vars["app_api_body"].get().strip() or "{}",
            "api_temp_key": self.vars["api_temp_key"].get().strip() or "data.temperature",
            "api_battery_key": self.vars["api_battery_key"].get().strip() or "data.battery",
            "api_signal_key": self.vars["api_signal_key"].get().strip() or "data.signal_dbm",
            "webhook_provider": self.vars["webhook_provider"].get().strip() or "wecom",
            "webhook_url": self.vars["webhook_url"].get().strip(),
            "apk_path": self.vars["apk_path"].get().strip(),
            "app_package": self.vars["app_package"].get().strip(),
            "app_activity": self.vars["app_activity"].get().strip(),
            "debug_always_on": bool(self.vars["debug_always_on"].get()),
        }

    def import_api_from_har(self) -> None:
        har_path = filedialog.askopenfilename(
            title="选择 HAR 抓包文件",
            filetypes=[("HAR File", "*.har"), ("JSON File", "*.json"), ("All Files", "*.*")],
        )
        if not har_path:
            return

        def _work() -> None:
            try:
                with open(har_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as exc:
                self.enqueue_log(f"[HAR] 读取失败: {exc}")
                return

            log_obj = data.get("log") if isinstance(data, dict) else None
            entries = log_obj.get("entries") if isinstance(log_obj, dict) else None
            if not isinstance(entries, list) or not entries:
                self.enqueue_log("[HAR] 文件中未找到有效 entries")
                return

            app_pkg = self.vars["app_package"].get().strip().lower()

            def _entry_score(entry: Dict[str, Any]) -> int:
                req = entry.get("request") if isinstance(entry, dict) else {}
                resp = entry.get("response") if isinstance(entry, dict) else {}
                if not isinstance(req, dict):
                    return -999
                method = str(req.get("method") or "").upper()
                url = str(req.get("url") or "")
                if method not in ("GET", "POST") or not url.startswith(("http://", "https://")):
                    return -999

                score = 0
                if app_pkg and app_pkg in url.lower():
                    score += 8
                if any(k in url.lower() for k in ("api", "inspection", "pasture", "monitor", "status")):
                    score += 4

                if isinstance(resp, dict):
                    content = resp.get("content")
                    if isinstance(content, dict):
                        mime = str(content.get("mimeType") or "").lower()
                        text = str(content.get("text") or "")
                        if "json" in mime:
                            score += 4
                        if any(k in text.lower() for k in ("temperature", "battery", "signal", "water", "temp")):
                            score += 4

                headers = req.get("headers")
                if isinstance(headers, list):
                    names = {str(h.get("name", "")).lower() for h in headers if isinstance(h, dict)}
                    if "authorization" in names:
                        score += 4
                    if "cookie" in names:
                        score += 2

                return score

            best = None
            best_score = -999
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                s = _entry_score(entry)
                if s > best_score:
                    best = entry
                    best_score = s

            if not isinstance(best, dict):
                self.enqueue_log("[HAR] 未找到可用 API 请求")
                return

            request_obj = best.get("request") if isinstance(best.get("request"), dict) else {}
            method = str(request_obj.get("method") or "GET").upper()
            url = str(request_obj.get("url") or "").strip()
            parsed = urlparse(url)
            base_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}" if parsed.scheme and parsed.netloc else url

            params: Dict[str, Any] = {}
            for k, values in parse_qs(parsed.query, keep_blank_values=True).items():
                if len(values) == 1:
                    params[k] = values[0]
                else:
                    params[k] = values

            post_body: Dict[str, Any] = {}
            post_data = request_obj.get("postData") if isinstance(request_obj.get("postData"), dict) else {}
            text_data = str(post_data.get("text") or "").strip() if isinstance(post_data, dict) else ""
            if text_data:
                try:
                    parsed_body = json.loads(text_data)
                    if isinstance(parsed_body, dict):
                        post_body = parsed_body
                except Exception:
                    post_body = {}

            headers_out: Dict[str, str] = {}
            headers = request_obj.get("headers") if isinstance(request_obj.get("headers"), list) else []
            excluded = {"host", "content-length", "connection", "accept-encoding"}
            for h in headers:
                if not isinstance(h, dict):
                    continue
                name = str(h.get("name") or "").strip()
                value = str(h.get("value") or "").strip()
                if not name or not value:
                    continue
                if name.lower() in excluded:
                    continue
                if len(value) > 3000:
                    continue
                headers_out[name] = value

            self.root.after(0, lambda: self.vars["api_enabled"].set(True))
            self.root.after(0, lambda: self.vars["app_api_method"].set(method if method in ("GET", "POST") else "GET"))
            self.root.after(0, lambda: self.vars["app_api_url"].set(base_url))
            self.root.after(0, lambda: self.vars["app_api_headers"].set(json.dumps(headers_out, ensure_ascii=False)))
            self.root.after(0, lambda: self.vars["app_api_params"].set(json.dumps(params, ensure_ascii=False)))
            self.root.after(0, lambda: self.vars["app_api_body"].set(json.dumps(post_body, ensure_ascii=False)))

            self.enqueue_log(f"[HAR] 已导入 API 配置: {method} {base_url}")
            self.enqueue_log(f"[HAR] headers={len(headers_out)} params={len(params)} body_keys={len(post_body)}")

        threading.Thread(target=_work, daemon=True).start()

    def open_advanced_settings(self) -> None:
        dlg = ttkb.Toplevel(self.root)
        dlg.title("高级设置与诊断工具")
        dlg.geometry("760x640")

        try:
            dlg.make_modal()
        except Exception:
            pass

        unlock_state = {"count": 0}

        try:
            notebook = ttk.Notebook(dlg)
            notebook.pack(fill=BOTH, expand=True, padx=10, pady=10)

            # 1. 阈值与参数设置
            f_thresh = ttkb.Frame(notebook, padding=15)
            notebook.add(f_thresh, text="巡检参数")

            def add_input(parent, row, label_text, var_name, width=20):
                ttkb.Label(parent, text=label_text).grid(row=row, column=0, sticky="e", pady=4, padx=4)
                ttkb.Entry(parent, textvariable=self.vars[var_name], width=width).grid(row=row, column=1, sticky="w", pady=4)

            add_input(f_thresh, 0, "巡检间隔 (秒):", "interval_sec")
            add_input(f_thresh, 1, "弱网重试次数:", "weak_network_retry")
            add_input(f_thresh, 2, "水温下限 (°C):", "water_temp_low")
            add_input(f_thresh, 3, "水温上限 (°C):", "water_temp_high")
            add_input(f_thresh, 4, "电量告警线 (%):", "battery_min")
            add_input(f_thresh, 5, "信号告警线 (dBm):", "signal_dbm_min")

            # 2. Webhook与API
            f_api = ttkb.Frame(notebook, padding=15)
            notebook.add(f_api, text="上报与API")

            f_webh = ttkb.Labelframe(f_api, text="Webhook设置", padding=10)
            f_webh.pack(fill=X, pady=5)
            ttkb.Label(f_webh, text="提供商:").grid(row=0, column=0, sticky="e")
            cb = ttk.Combobox(
                f_webh,
                textvariable=self.vars["webhook_provider"],
                values=["wecom", "dingtalk"],
                state="readonly",
                width=18,
            )
            cb.grid(row=0, column=1, sticky="w", padx=4)

            ttkb.Label(f_webh, text="URL:").grid(row=1, column=0, sticky="e", pady=4)
            ttkb.Entry(f_webh, textvariable=self.vars["webhook_url"], width=60).grid(row=1, column=1, sticky="w", padx=4)

            f_req = ttkb.Labelframe(f_api, text="外部API同步 (如需)", padding=10)
            f_req.pack(fill=X, pady=5)
            ttkb.Checkbutton(
                f_req,
                text="启用API数据推送",
                variable=self.vars["api_enabled"],
                bootstyle="round-toggle",
            ).grid(row=0, column=0, columnspan=2, sticky="w", pady=4)
            add_input(f_req, 1, "API URL:", "app_api_url", 60)
            ttkb.Label(f_req, text="Method:").grid(row=2, column=0, sticky="e", pady=4, padx=4)
            ttk.Combobox(f_req, textvariable=self.vars["app_api_method"], values=["GET", "POST"], state="readonly", width=18).grid(
                row=2, column=1, sticky="w", pady=4
            )
            add_input(f_req, 3, "Headers(JSON):", "app_api_headers", 60)
            add_input(f_req, 4, "Params(JSON):", "app_api_params", 60)
            add_input(f_req, 5, "Body(JSON):", "app_api_body", 60)

            f_map = ttkb.Labelframe(f_api, text="API 字段映射", padding=10)
            f_map.pack(fill=X, pady=5)
            add_input(f_map, 0, "温度字段:", "api_temp_key", 36)
            add_input(f_map, 1, "电量字段:", "api_battery_key", 36)
            add_input(f_map, 2, "信号字段:", "api_signal_key", 36)

            # 3. 诊断与维护工具
            f_tools = ttkb.Frame(notebook, padding=15)
            notebook.add(f_tools, text="维护工具")

            ttkb.Label(f_tools, text="点击下方按钮运行相应的维护或诊断任务", bootstyle="secondary").pack(anchor="w", pady=(0,10))
            ttkb.Button(f_tools, text="解析APK接口", bootstyle="secondary-outline", command=self.analyze_apk, width=20).pack(pady=5, anchor="w")
            ttkb.Button(f_tools, text="从HAR导入API", bootstyle="secondary-outline", command=self.import_api_from_har, width=20).pack(pady=5, anchor="w")
            ttkb.Button(f_tools, text="识别本地图片 (OCR测试)", bootstyle="secondary-outline", command=self.recognize_local_image, width=20).pack(pady=5, anchor="w")
            ttkb.Button(f_tools, text="手动执行ADB诊断", bootstyle="primary-outline", command=self.adb_diagnose, width=20).pack(pady=5, anchor="w")

            # 4. 隐藏调试页（连击解锁）
            debug_vars = {
                "connected": tk.StringVar(value="1"),
                "device": tk.StringVar(value="调试设备"),
                "temperature": tk.StringVar(value="0.0"),
                "battery": tk.StringVar(value="80"),
                "signal_dbm": tk.StringVar(value="-80"),
                "ear_online_rate": tk.StringVar(value="95"),
                "task_done": tk.StringVar(value="8"),
                "task_total": tk.StringVar(value="12"),
                "coverage_rate": tk.StringVar(value="66"),
                "alert_rate": tk.StringVar(value="92"),
                "ocr_accuracy": tk.StringVar(value="97"),
                "avg_response": tk.StringVar(value="95"),
                "note": tk.StringVar(value="调试注入"),
            }
            f_debug = ttkb.Frame(notebook, padding=15)

            def _unlock_debug(_event=None) -> None:
                unlock_state["count"] += 1
                if unlock_state["count"] < 5:
                    return
                if str(f_debug) in notebook.tabs():
                    notebook.select(f_debug)
                    return
                notebook.add(f_debug, text="调试注入")
                notebook.select(f_debug)

            ttkb.Label(
                f_debug,
                text="手动输入指标并立即渲染到仪表盘（仅调试使用）",
                bootstyle="warning",
            ).pack(anchor="w", pady=(0, 8))
            ttkb.Checkbutton(
                f_debug,
                text="调试模式常开（启动巡检时持续覆盖展示数据）",
                variable=self.vars["debug_always_on"],
                bootstyle="round-toggle",
            ).pack(anchor="w", pady=(0, 8))

            debug_form = ttkb.Frame(f_debug)
            debug_form.pack(fill=X)

            def add_debug_input(row: int, label: str, key: str) -> None:
                ttkb.Label(debug_form, text=label).grid(row=row, column=0, sticky="e", padx=4, pady=4)
                ttkb.Entry(debug_form, textvariable=debug_vars[key], width=20).grid(row=row, column=1, sticky="w", padx=4, pady=4)

            add_debug_input(0, "温度(°C):", "temperature")
            add_debug_input(1, "电量(%):", "battery")
            add_debug_input(2, "信号(dBm):", "signal_dbm")
            add_debug_input(3, "耳标在线率(%):", "ear_online_rate")
            add_debug_input(4, "任务完成数:", "task_done")
            add_debug_input(5, "任务总数:", "task_total")
            add_debug_input(6, "覆盖率(%):", "coverage_rate")
            add_debug_input(7, "告警检出率(%):", "alert_rate")
            add_debug_input(8, "OCR准确率(%):", "ocr_accuracy")
            add_debug_input(9, "平均响应(%):", "avg_response")
            add_debug_input(10, "设备标识:", "device")
            add_debug_input(11, "连接状态(1在线/0离线):", "connected")
            add_debug_input(12, "备注:", "note")

            dbg_btns = ttkb.Frame(f_debug)
            dbg_btns.pack(fill=X, pady=(10, 0))
            ttkb.Button(
                dbg_btns,
                text="启动调试",
                bootstyle="warning",
                command=lambda: self._start_debug_mode(debug_vars),
            ).pack(side=LEFT)
            ttkb.Button(
                dbg_btns,
                text="关闭调试",
                bootstyle="secondary-outline",
                command=self._stop_debug_mode,
            ).pack(side=LEFT, padx=8)
            ttkb.Button(
                dbg_btns,
                text="触发调试告警",
                bootstyle="danger-outline",
                command=lambda: self._push_alert("warning", "调试告警", f"手动调试: {debug_vars['note'].get().strip() or '无备注'}"),
            ).pack(side=LEFT, padx=8)

            secret = ttkb.Label(dlg, text="v1.0.5", bootstyle="secondary")
            secret.pack(side=LEFT, padx=(12, 0), pady=(0, 10))
            secret.bind("<Button-1>", _unlock_debug)

            # 底部保存按钮
            btn_frame = ttkb.Frame(dlg, padding=10)
            btn_frame.pack(fill=X, side=tk.BOTTOM)
            ttkb.Button(btn_frame, text="关闭并保存配置", bootstyle="primary", command=lambda: [self.save_config(), dlg.destroy()]).pack(side=RIGHT)
        except Exception as exc:
            self.enqueue_log(f"[高级设置] 打开失败: {exc}")
            messagebox.showerror("高级设置异常", f"高级设置界面初始化失败:\n{exc}")
            try:
                dlg.destroy()
            except Exception:
                pass

    def _apply_debug_metrics(self, debug_vars: Dict[str, tk.StringVar]) -> None:
        try:
            temp = float(debug_vars["temperature"].get().strip())
            battery = int(debug_vars["battery"].get().strip())
            signal = int(debug_vars["signal_dbm"].get().strip())
            ear_rate = int(debug_vars["ear_online_rate"].get().strip())
            task_done = int(debug_vars["task_done"].get().strip())
            task_total = int(debug_vars["task_total"].get().strip())
        except Exception as exc:
            messagebox.showerror("调试输入错误", f"请输入有效数值: {exc}")
            return

        task_total = max(1, task_total)
        task_done = min(max(0, task_done), task_total)
        note = debug_vars["note"].get().strip() or "调试注入"

        payload = {
            "connected": True,
            "device": self.vars["target_device"].get().strip() or "调试设备",
            "metrics": {
                "temperature": temp,
                "battery": battery,
                "signal_dbm": signal,
                "ear_online_rate": ear_rate,
                "task_progress": f"{task_done}/{task_total}",
            },
            "task_completed": task_done,
            "task_total": task_total,
            "healthy_rounds": 0,
            "note": f"[调试] {note}",
        }
        self.apply_metrics(payload)
        self.enqueue_log(f"[调试] 已注入指标: 温度={temp}, 电量={battery}, 信号={signal}, 任务={task_done}/{task_total}")

    def _resolve_runtime_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if bool(self.vars["debug_always_on"].get()) and self.debug_payload:
            return copy.deepcopy(self.debug_payload)
        return payload

    def _build_debug_payload(self, debug_vars: Dict[str, tk.StringVar]) -> Dict[str, Any]:
        connected_flag = int(debug_vars["connected"].get().strip())
        device = debug_vars["device"].get().strip() or "调试设备"
        temp = float(debug_vars["temperature"].get().strip())
        battery = int(debug_vars["battery"].get().strip())
        signal = int(debug_vars["signal_dbm"].get().strip())
        ear_rate = int(debug_vars["ear_online_rate"].get().strip())
        task_done = int(debug_vars["task_done"].get().strip())
        task_total = int(debug_vars["task_total"].get().strip())
        coverage_rate = float(debug_vars["coverage_rate"].get().strip())
        alert_rate = float(debug_vars["alert_rate"].get().strip())
        ocr_accuracy = float(debug_vars["ocr_accuracy"].get().strip())
        avg_response = float(debug_vars["avg_response"].get().strip())

        task_total = max(1, task_total)
        task_done = min(max(0, task_done), task_total)
        coverage_rate = max(0, min(100, coverage_rate))
        alert_rate = max(0, min(100, alert_rate))
        ocr_accuracy = max(0, min(100, ocr_accuracy))
        avg_response = max(0, min(100, avg_response))
        note = debug_vars["note"].get().strip() or "调试注入"

        return {
            "connected": bool(connected_flag),
            "device": device,
            "metrics": {
                "temperature": temp,
                "battery": battery,
                "signal_dbm": signal,
                "ear_online_rate": ear_rate,
                "task_progress": f"{task_done}/{task_total}",
                "coverage_rate": coverage_rate,
                "alert_rate": alert_rate,
                "ocr_accuracy": ocr_accuracy,
                "avg_response": avg_response,
            },
            "task_completed": task_done,
            "task_total": task_total,
            "healthy_rounds": 0,
            "note": f"[调试] {note}",
        }

    def _start_debug_mode(self, debug_vars: Dict[str, tk.StringVar]) -> None:
        try:
            payload = self._build_debug_payload(debug_vars)
        except Exception as exc:
            messagebox.showerror("调试输入错误", f"请输入有效数值: {exc}")
            return

        self.debug_payload = payload
        self.debug_mode_enabled = True
        self.vars["debug_always_on"].set(True)
        self.apply_metrics(copy.deepcopy(self.debug_payload))

    def _stop_debug_mode(self) -> None:
        self.debug_mode_enabled = False
        self.debug_payload = {}
        self.vars["debug_always_on"].set(False)


    def choose_apk_file(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 APK 文件",
            filetypes=[("Android APK", "*.apk"), ("All Files", "*.*")],
        )
        if not path:
            return
        self.vars["apk_path"].set(path)
        self.enqueue_log(f"[APK] 已选择: {path}")

    def recognize_local_image(self) -> None:
        image_path = filedialog.askopenfilename(
            title="选择要识别的图片",
            filetypes=[("Image Files", "*.png;*.jpg;*.jpeg;*.bmp;*.webp"), ("All Files", "*.*")],
        )
        if not image_path:
            return

        def _work() -> None:
            ocr = SimpleOCR(adb_controller=None)
            metrics = ocr.extract_metrics_from_file(image_path)
            temp = metrics.get("temperature")
            battery = metrics.get("battery")
            signal = metrics.get("signal_dbm")
            ear_rate = metrics.get("ear_online_rate")
            task_progress = metrics.get("task_progress")

            payload = {
                "connected": False,
                "device": "本地图片",
                "metrics": {
                    "temperature": temp,
                    "battery": battery,
                    "signal_dbm": signal,
                },
                "task_completed": int(str(task_progress).split("/")[0]) if isinstance(task_progress, str) and "/" in task_progress else 0,
                "task_total": int(str(task_progress).split("/")[1]) if isinstance(task_progress, str) and "/" in task_progress else 12,
                "healthy_rounds": 0,
                "note": f"本地图片识别: {image_path}",
            }

            self.root.after(0, lambda: self.apply_metrics(payload))

            lines = [
                f"> [本地图片识别] {image_path}",
                f"> 温度: {temp if temp is not None else '--'}",
                f"> 电量: {str(battery) + '%' if battery is not None else '--'}",
                f"> 信号: {str(signal) + 'dBm' if signal is not None else '--'}",
                f"> 耳标在线率: {str(ear_rate) + '%' if ear_rate is not None else '--'}",
                f"> 巡检任务: {task_progress if isinstance(task_progress, str) else '--'}",
                f"> 原始识别: {metrics.get('raw_text') or '--'}",
            ]
            self.root.after(0, lambda: self.ocr_text.delete("1.0", END))
            self.root.after(0, lambda: self.ocr_text.insert(END, "\n".join(lines) + "\n"))
            self.enqueue_log(f"[OCR] 本地图片识别完成: {image_path}")

        threading.Thread(target=_work, daemon=True).start()

    def analyze_apk(self) -> None:
        apk_path = self.vars["apk_path"].get().strip()
        if not apk_path:
            messagebox.showinfo("未选择APK", "请先选择 APK 文件。")
            return

        def _work() -> None:
            info = analyze_apk_interfaces(apk_path)
            if not info.get("ok"):
                self.enqueue_log(f"[APK] 解析失败: {info.get('error')}")
                return

            package_name = str(info.get("package_name") or "")
            package_confidence = float(info.get("package_confidence") or 0.0)
            package_candidates = list(info.get("package_candidates") or [])
            launch_activity = str(info.get("launch_activity") or "")
            launch_candidates = list(info.get("launch_activity_candidates") or [])
            if package_name and package_confidence >= 0.55:
                self.root.after(0, lambda: self.vars["app_package"].set(package_name))

            chosen_activity = ""
            if self._is_probable_launch_activity(launch_activity):
                chosen_activity = launch_activity
            else:
                for item in launch_candidates:
                    if isinstance(item, str) and self._is_probable_launch_activity(item):
                        chosen_activity = item
                        break
            if chosen_activity:
                self.root.after(0, lambda: self.vars["app_activity"].set(chosen_activity))

            self.enqueue_log(f"[APK] 包名: {package_name or '未识别'} (置信度 {package_confidence:.2f})")
            self.enqueue_log(f"[APK] 入口Activity: {chosen_activity or launch_activity or '未识别'}")
            if package_candidates:
                self.enqueue_log(f"[APK] 包名候选: {', '.join(package_candidates[:3])}")

            http_endpoints = list(info.get("http_endpoints") or [])
            ws_endpoints = list(info.get("ws_endpoints") or [])
            uri_schemes = list(info.get("uri_schemes") or [])

            preview_lines = [
                f"> APK: {apk_path}",
                f"> 包名: {package_name or '未识别'}",
                f"> 置信度: {package_confidence:.2f}",
                f"> 入口: {launch_activity or '未识别'}",
                f"> HTTP接口: {len(http_endpoints)} 条",
                f"> WS接口: {len(ws_endpoints)} 条",
                f"> Deeplink Scheme: {', '.join(uri_schemes[:6]) if uri_schemes else '无'}",
            ]
            if http_endpoints:
                preview_lines.append("> 示例HTTP: " + http_endpoints[0])
            if ws_endpoints:
                preview_lines.append("> 示例WS: " + ws_endpoints[0])

            self.root.after(0, lambda: self.ocr_text.delete("1.0", END))
            self.root.after(0, lambda: self.ocr_text.insert(END, "\n".join(preview_lines) + "\n"))

        threading.Thread(target=_work, daemon=True).start()

    def install_and_launch_app(self) -> None:
        serial = self.vars["target_device"].get().strip()
        if not serial:
            messagebox.showinfo("未连接设备", "请先扫描并连接设备。")
            return

        apk_path = self.vars["apk_path"].get().strip()
        package_name = self.vars["app_package"].get().strip()
        activity_name = self.vars["app_activity"].get().strip()

        if not apk_path and not package_name:
            messagebox.showinfo("参数不足", "请至少提供 APK 文件或包名。")
            return

        adb_path = self.vars["adb_path"].get().strip()

        def _work() -> None:
            nonlocal package_name, activity_name
            controller = AdbController(target_device=serial)
            if adb_path:
                controller.adb_path = adb_path

            if not controller.connect(serial):
                controller.close()
                self.enqueue_log(f"[接入] 设备连接失败: {serial}")
                return

            if apk_path:
                ok, msg = controller.install_apk(apk_path, replace=True)
                self.enqueue_log(f"[接入] APK安装: {'成功' if ok else '失败'} - {msg}")
                if not ok:
                    controller.close()
                    return

            if not package_name and apk_path:
                info = analyze_apk_interfaces(apk_path)
                package_name = str(info.get("package_name") or "")
                if package_name:
                    self.root.after(0, lambda: self.vars["app_package"].set(package_name))
                guessed_activity = str(info.get("launch_activity") or "")
                if guessed_activity and not activity_name and self._is_probable_launch_activity(guessed_activity):
                    activity_name = guessed_activity
                    self.root.after(0, lambda: self.vars["app_activity"].set(activity_name))

            if activity_name and not self._is_probable_launch_activity(activity_name):
                self.enqueue_log(f"[接入] 忽略无效Activity候选: {activity_name}")
                activity_name = ""

            if package_name:
                resolved_activity = controller.resolve_launcher_activity(package_name)
                if resolved_activity:
                    activity_name = resolved_activity
                    self.root.after(0, lambda: self.vars["app_activity"].set(activity_name))
                    self.enqueue_log(f"[接入] 真实入口Activity: {resolved_activity}")

            if not package_name:
                self.enqueue_log("[接入] 无法确定包名，已完成安装但未启动")
                controller.close()
                return

            ok, msg = controller.launch_app(package_name, activity_name or None)
            if (not ok) and ("offline" in (msg or "").lower()):
                self.enqueue_log("[接入] 设备离线，尝试重连后再次启动")
                if controller.reconnect():
                    ok, msg = controller.launch_app(package_name, activity_name or None)
            self.enqueue_log(f"[接入] 应用启动: {'成功' if ok else '失败'} - {msg}")
            controller.close()

        threading.Thread(target=_work, daemon=True).start()

    def auto_fill_adb(self) -> None:
        current = self.vars["adb_path"].get().strip() or None
        adb_path, message = select_working_adb_path(current)
        self.vars["adb_path"].set(adb_path)
        self.enqueue_log(f"[配置] ADB 已就绪: {adb_path}")
        if message:
            self.enqueue_log(f"[配置] {message.splitlines()[0]}")

    def scan_devices(self) -> None:
        adb_path = self.vars["adb_path"].get().strip()

        def _work() -> None:
            info = discover_device_entries(adb_path=adb_path or None)
            resolved_adb = str(info.get("adb_path") or adb_path or CURRENT_ADB_PATH)
            set_custom_adb_path(resolved_adb)
            self.vars["adb_path"].set(resolved_adb)
            self.discovered_devices = list(info.get("devices") or [])

            labels = [str(item.get("label") or item.get("serial") or "") for item in self.discovered_devices]
            self.root.after(0, lambda: self.device_combo.configure(values=labels))

            if self.discovered_devices:
                first_serial = str(self.discovered_devices[0].get("serial") or "")
                self.root.after(0, lambda: self.device_display_var.set(labels[0]))
                self.root.after(0, lambda: self.vars["target_device"].set(first_serial))
                self.enqueue_log(f"[设备] 发现 {len(self.discovered_devices)} 台设备，已自动选中首台")
                self._render_device_entries("扫描完成，等待连接")
                self.connect_device(first_serial, silent=False)
            else:
                self.root.after(0, lambda: self.device_display_var.set(""))
                self.enqueue_log("[设备] 未发现在线设备")
                self._render_devices(self.vars["target_device"].get().strip() or "未指定", False, "未发现在线设备")

        threading.Thread(target=_work, daemon=True).start()

    def _on_device_selected(self, _event=None) -> None:
        selected = self.device_combo.get().strip()
        for item in self.discovered_devices:
            if selected == str(item.get("label") or ""):
                self.vars["target_device"].set(str(item.get("serial") or ""))
                self._render_device_entries("已选择设备，等待连接")
                break

    def _render_device_entries(self, note: str) -> None:
        if not self.discovered_devices:
            self._render_devices(self.vars["target_device"].get().strip() or "未指定", False, note)
            return
        lines = ["[设备发现结果]"]
        for idx, item in enumerate(self.discovered_devices, start=1):
            lines.append(f"{idx}) {item.get('label')} · 状态:{item.get('status')}")
        lines.append(f"备注: {note}")
        self.device_text.delete("1.0", END)
        self.device_text.insert(END, "\n".join(lines))

    def connect_device(self, serial: str, silent: bool = True) -> None:
        adb_path = self.vars["adb_path"].get().strip()
        if adb_path:
            set_custom_adb_path(adb_path)

        def _work() -> None:
            controller = AdbController(target_device=serial)
            if adb_path:
                controller.adb_path = adb_path
            ok = controller.connect(serial)
            controller.close()
            if ok:
                self.vars["target_device"].set(serial)
                self.enqueue_log(f"[设备] 已连接: {serial}")
                self._render_device_entries("设备连接成功")
            else:
                self.enqueue_log(f"[设备] 连接失败: {serial}")
                self._render_device_entries("设备连接失败")
                if not silent:
                    self.root.after(0, lambda: messagebox.showwarning("连接失败", f"无法连接设备: {serial}"))

        threading.Thread(target=_work, daemon=True).start()

    def connect_selected_device(self) -> None:
        serial = self.vars["target_device"].get().strip()
        if not serial:
            messagebox.showinfo("未选择设备", "请先扫描设备并选择一个目标设备。")
            return
        self.connect_device(serial, silent=False)

    def adb_diagnose(self) -> None:
        adb_path = self.vars["adb_path"].get().strip()

        def _work() -> None:
            try:
                resolved, _ = select_working_adb_path(adb_path or None)
                set_custom_adb_path(resolved)
                self.vars["adb_path"].set(resolved)
                ok, version = probe_adb_version(resolved)
                if not ok:
                    self.enqueue_log(f"[ADB诊断] adb 不可用: {version}")
                    return
                serials = AdbController.list_devices(adb_path=resolved)
                if serials:
                    self.enqueue_log(f"[ADB诊断] 已连接 adb: {resolved}")
                    self.enqueue_log(f"[ADB诊断] 在线设备: {', '.join(serials)}")
                else:
                    self.enqueue_log(f"[ADB诊断] adb 正常但未发现设备: {resolved}")
            except Exception as exc:
                self.enqueue_log(f"[ADB诊断] 失败: {exc}")

        threading.Thread(target=_work, daemon=True).start()

    def run_system_self_check(self) -> None:
        adb_path = self.vars["adb_path"].get().strip()
        target = self.vars["target_device"].get().strip()
        webhook = self.vars["webhook_url"].get().strip()

        def _work() -> None:
            checks: List[str] = []
            ok_all = True
            resolved_adb = adb_path
            target_serial = target

            self.enqueue_log("[自检] 开始执行系统自检（ADB/设备/OCR/Webhook）")

            # 1) ADB 可用性
            try:
                resolved_adb, _ = select_working_adb_path(adb_path or None)
                set_custom_adb_path(resolved_adb)
                self.root.after(0, lambda: self.vars["adb_path"].set(resolved_adb))
                adb_ok, version = probe_adb_version(resolved_adb)
                if adb_ok:
                    checks.append(f"ADB: OK ({resolved_adb})")
                else:
                    checks.append(f"ADB: FAIL ({version})")
                    ok_all = False
            except Exception as exc:
                checks.append(f"ADB: FAIL ({exc})")
                ok_all = False

            serials: List[str] = []
            # 2) 设备连通
            if ok_all:
                try:
                    serials = AdbController.list_devices(adb_path=resolved_adb)
                    if not serials:
                        checks.append("设备: FAIL (未发现在线设备)")
                        ok_all = False
                    else:
                        if not target:
                            target_candidate = serials[0]
                            self.root.after(0, lambda: self.vars["target_device"].set(target_candidate))
                            target_local = target_candidate
                        else:
                            target_local = target_serial
                        if target_local in serials:
                            checks.append(f"设备: OK ({target_local})")
                            target_serial = target_local
                        else:
                            checks.append(f"设备: FAIL (目标 {target_local} 不在线)")
                            ok_all = False
                except Exception as exc:
                    checks.append(f"设备: FAIL ({exc})")
                    ok_all = False

            # 3) 截图 + OCR 基础链路
            if ok_all and target_serial:
                controller: AdbController | None = None
                try:
                    controller = AdbController(target_device=target_serial)
                    controller.adb_path = resolved_adb
                    if not controller.connect(target_serial):
                        checks.append("截图/OCR: FAIL (连接设备失败)")
                        ok_all = False
                    else:
                        screen = controller.get_screenshot()
                        if screen is None:
                            checks.append("截图/OCR: FAIL (截图失败)")
                            ok_all = False
                        else:
                            ocr = SimpleOCR(adb_controller=controller)
                            metrics = ocr.extract_metrics(screen=screen)
                            if any(metrics.get(k) is not None for k in ("temperature", "battery", "signal_dbm", "task_progress")):
                                checks.append("截图/OCR: OK (识别链路可用)")
                            else:
                                checks.append("截图/OCR: WARN (截图成功，但关键指标为空)")
                except Exception as exc:
                    checks.append(f"截图/OCR: FAIL ({exc})")
                    ok_all = False
                finally:
                    if controller:
                        try:
                            controller.close()
                        except Exception:
                            pass

            # 4) Webhook 可达性
            if webhook:
                try:
                    resp = requests.post(
                        webhook,
                        json={
                            "msgtype": "markdown",
                            "markdown": {"content": "### 自检连通性测试\n> 此消息用于验证Webhook可达性。"},
                        },
                        timeout=6,
                    )
                    if 200 <= resp.status_code < 300:
                        checks.append("Webhook: OK")
                    else:
                        checks.append(f"Webhook: FAIL (HTTP {resp.status_code})")
                        ok_all = False
                except Exception as exc:
                    checks.append(f"Webhook: FAIL ({exc})")
                    ok_all = False
            else:
                checks.append("Webhook: SKIP (未配置)")

            summary = "\n".join(f"- {line}" for line in checks)
            self.enqueue_log("[自检] 结果汇总:\n" + summary)
            if ok_all:
                self.ui_queue.put(("alert", {"level": "warning", "title": "系统自检通过", "message": "各核心链路已通过检查"}))
                self.root.after(0, lambda: messagebox.showinfo("系统自检", "自检通过\n\n" + summary))
            else:
                self.ui_queue.put(("alert", {"level": "critical", "title": "系统自检异常", "message": "存在未通过项，请查看日志"}))
                self.root.after(0, lambda: messagebox.showwarning("系统自检", "存在异常项\n\n" + summary))

        threading.Thread(target=_work, daemon=True).start()

    def load_config(self) -> None:
        cfg = load_config_from_file(CONFIG_FILE)
        if not cfg:
            self.enqueue_log("[配置] 使用默认配置")
            return

        for k, var in self.vars.items():
            if k in cfg:
                value = cfg.get(k, "")
                if isinstance(var, tk.BooleanVar):
                    var.set(bool(value))
                else:
                    var.set(str(value))

        # 若仅从配置恢复到“调试常开”但无调试载荷，自动回退，避免界面看似开启但无数据。
        if bool(self.vars["debug_always_on"].get()) and not self.debug_payload:
            self.vars["debug_always_on"].set(False)
            self.enqueue_log("[配置] 调试常开已自动关闭（未检测到调试输入载荷）")

        apk_path = self.vars["apk_path"].get().strip()
        if not apk_path or not os.path.isfile(apk_path):
            local_apk = os.path.join(os.path.dirname(os.path.abspath(__file__)), "智巡草原-已修复.apk")
            if os.path.isfile(local_apk):
                self.vars["apk_path"].set(local_apk)
                self.enqueue_log(f"[配置] 自动使用本地APK: {local_apk}")

        app_activity = self.vars["app_activity"].get().strip()
        if app_activity and not self._is_probable_launch_activity(app_activity):
            self.vars["app_activity"].set("")
            self.enqueue_log("[配置] 已清理无效Activity，启动时将自动解析")

        self.enqueue_log(f"[配置] 已加载 {CONFIG_FILE}")

    def save_config(self, silent: bool = False) -> bool:
        try:
            payload = self.collect_config()
        except Exception as exc:
            messagebox.showerror("配置错误", f"参数格式错误: {exc}")
            return False

        ok = save_config_to_file(CONFIG_FILE, payload)
        if not ok:
            messagebox.showerror("保存失败", "写入配置文件失败")
            return False

        if not silent:
            messagebox.showinfo("保存成功", f"配置已保存到 {CONFIG_FILE}")
        self.enqueue_log("[配置] 已保存")
        return True

    def start_bot(self) -> None:
        if not self.save_config(silent=True):
            return

        if bool(self.vars["debug_always_on"].get()) and self.debug_payload:
            self.bot.stop()
            self.apply_metrics(copy.deepcopy(self.debug_payload))
            self.enqueue_log("[调试] 调试模式常开：已使用调试输入值运行")
            return

        set_custom_adb_path(self.vars["adb_path"].get().strip())
        try:
            self.bot.start()
        except Exception as exc:
            messagebox.showerror("启动失败", str(exc))

    def stop_bot(self) -> None:
        self.bot.stop()

    def refresh_dashboard(self) -> None:
        if bool(self.vars["debug_always_on"].get()) and self.debug_payload:
            self.apply_metrics(copy.deepcopy(self.debug_payload))
            return
        if self.latest_payload:
            self.apply_metrics(self.latest_payload)
            return
        self.simulate_inspection()

    def simulate_inspection(self) -> None:
        payload = {
            "connected": True,
            "device": self.vars["target_device"].get().strip() or "192.168.8.112:5555",
            "metrics": {
                "temperature": round(random.uniform(-2.2, 2.0), 1),
                "battery": random.randint(70, 98),
                "signal_dbm": random.randint(-90, -65),
            },
            "task_completed": random.randint(8, 12),
            "task_total": 12,
            "healthy_rounds": random.randint(1, 10),
            "note": "模拟巡检完成",
        }
        self.apply_metrics(payload)
        self._push_alert("warning", "巡检完成", "系统自愈检测通过，节点在线")

    def simulate_alert(self) -> None:
        pool = [
            ("critical", "饮水槽低温告警", "当前水温 -2.3°C，建议检查加热带"),
            ("warning", "耳标离线告警", "2个耳标离线超过15分钟，请巡场确认"),
            ("critical", "终端电量告警", "气象站节点电量低于20%"),
            ("warning", "弱网重连", "节点自动重连成功，任务继续运行"),
        ]
        level, title, message = random.choice(pool)
        self._push_alert(level, title, message)

    def on_close(self) -> None:
        self._is_closing = True
        try:
            self.bot.stop()
        except Exception:
            pass
        try:
            self.root.destroy()
        except TclError:
            pass


def main() -> None:
    app = App()
    app.root.mainloop()


if __name__ == "__main__":
    main()
