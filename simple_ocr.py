from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np


class StopSignal(Exception):
    pass


@dataclass
class OCRField:
    name: str
    region: Tuple[int, int, int, int]
    pattern: str


class SimpleOCR:
    """
    基于 icon 卡片模板的轻量识别器。
    - 先定位卡片
    - 再识别大号数值
    """

    CARD_SAMPLES = {
        "water": ("饮水槽监测.png", "-2.3°C"),
        "ear": ("耳标在线率.png", "94%"),
        "health": ("终端健康度.png", "87%"),
        "task": ("巡检任务.png", "8/12"),
    }

    def __init__(self, adb_controller=None, icon_path: str = "") -> None:
        self.adb = adb_controller
        self.icon_path = icon_path
        self.card_templates: Dict[str, np.ndarray] = {}
        self.card_value_rel_box: Dict[str, Tuple[float, float, float, float]] = {}
        self.char_templates: Dict[str, List[np.ndarray]] = {}
        self.numeric_templates = self._build_numeric_templates()
        self._load_icon_templates()

    @staticmethod
    def _build_numeric_templates() -> Dict[str, List[np.ndarray]]:
        chars = "0123456789-./%"
        templates: Dict[str, List[np.ndarray]] = {ch: [] for ch in chars}
        fonts = [
            cv2.FONT_HERSHEY_SIMPLEX,
            cv2.FONT_HERSHEY_DUPLEX,
            cv2.FONT_HERSHEY_COMPLEX,
        ]
        for ch in chars:
            for font in fonts:
                for scale in (0.8, 1.0, 1.2, 1.4):
                    for thickness in (1, 2, 3):
                        canvas = np.zeros((64, 48), dtype=np.uint8)
                        cv2.putText(canvas, ch, (6, 50), font, scale, 255, thickness, cv2.LINE_AA)
                        _, bw = cv2.threshold(canvas, 10, 255, cv2.THRESH_BINARY)
                        templates[ch].append(bw)
        return templates

    def _resolve_icon_dir(self) -> str:
        candidates = []
        if self.icon_path:
            candidates.append(self.icon_path)
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon"))
        for p in candidates:
            if os.path.isdir(p):
                return p
        return ""

    @staticmethod
    def _read_image(path: str) -> Optional[np.ndarray]:
        if not path or not os.path.isfile(path):
            return None
        try:
            raw = np.fromfile(path, dtype=np.uint8)
            if raw.size == 0:
                return None
            return cv2.imdecode(raw, cv2.IMREAD_COLOR)
        except Exception:
            return None

    @staticmethod
    def _to_gray(img: np.ndarray) -> np.ndarray:
        if img.ndim == 2:
            return img
        return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    @staticmethod
    def _binarize(img: np.ndarray) -> np.ndarray:
        gray = SimpleOCR._to_gray(img)
        blur = cv2.GaussianBlur(gray, (3, 3), 0)
        return cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 31, 7)

    @staticmethod
    def _binarize_inv(img: np.ndarray) -> np.ndarray:
        gray = SimpleOCR._to_gray(img)
        blur = cv2.GaussianBlur(gray, (3, 3), 0)
        return cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 7)

    @staticmethod
    def _find_char_boxes(binary: np.ndarray) -> List[Tuple[int, int, int, int]]:
        contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        boxes: List[Tuple[int, int, int, int]] = []
        h_img, w_img = binary.shape[:2]
        for c in contours:
            x, y, w, h = cv2.boundingRect(c)
            area = w * h
            if area < 20:
                continue
            if h < max(8, int(h_img * 0.08)):
                continue
            if w < 2:
                continue
            if y > h_img * 0.9 or x > w_img * 0.98:
                continue
            boxes.append((x, y, w, h))
        boxes.sort(key=lambda b: b[0])
        return boxes

    def _load_icon_templates(self) -> None:
        icon_dir = self._resolve_icon_dir()
        if not icon_dir:
            return

        for key, (filename, sample_text) in self.CARD_SAMPLES.items():
            full = os.path.join(icon_dir, filename)
            if not os.path.isfile(full):
                continue
            raw = np.fromfile(full, dtype=np.uint8)
            tpl = cv2.imdecode(raw, cv2.IMREAD_COLOR)
            if tpl is None or tpl.size == 0:
                continue
            self.card_templates[key] = tpl
            rel_box = self._learn_value_box_and_chars(tpl, sample_text)
            if rel_box is not None:
                self.card_value_rel_box[key] = rel_box

    def _learn_value_box_and_chars(self, card_img: np.ndarray, sample_text: str) -> Optional[Tuple[float, float, float, float]]:
        h, w = card_img.shape[:2]
        # 数值大字通常位于卡片中上部区域。
        y1, y2 = int(h * 0.22), int(h * 0.63)
        x1, x2 = int(w * 0.08), int(w * 0.92)
        roi = card_img[y1:y2, x1:x2]
        binary = self._binarize(roi)
        boxes = self._find_char_boxes(binary)
        if not boxes:
            return None

        # 取面积较大的字符，避免把底部说明文字带进去。
        boxes = [b for b in boxes if b[3] >= int((y2 - y1) * 0.35)] or boxes
        if not boxes:
            return None

        ux1 = min(b[0] for b in boxes)
        uy1 = min(b[1] for b in boxes)
        ux2 = max(b[0] + b[2] for b in boxes)
        uy2 = max(b[1] + b[3] for b in boxes)

        value_roi = binary[uy1:uy2, ux1:ux2]
        value_boxes = self._find_char_boxes(value_roi)

        # 仅保留可用于数值解析的字符，避免 "°C" 这类多字形导致长度失配。
        sample_chars = [ch for ch in sample_text if ch.isdigit() or ch in {"-", ".", "/", "%"}]
        if value_boxes and sample_chars:
            n = min(len(value_boxes), len(sample_chars))
            for i in range(n):
                ch = sample_chars[i]
                x, y, cw, chh = value_boxes[i]
                glyph = value_roi[y:y + chh, x:x + cw]
                if glyph.size == 0:
                    continue
                resized = cv2.resize(glyph, (32, 48), interpolation=cv2.INTER_CUBIC)
                self.char_templates.setdefault(ch, []).append(resized)

        abs_x1 = x1 + ux1
        abs_y1 = y1 + uy1
        abs_x2 = x1 + ux2
        abs_y2 = y1 + uy2

        return (abs_x1 / w, abs_y1 / h, abs_x2 / w, abs_y2 / h)

    def _match_card(self, screen: np.ndarray, card_key: str) -> Optional[Tuple[int, int, int, int, float]]:
        tpl = self.card_templates.get(card_key)
        if tpl is None:
            return None

        screen_gray = self._to_gray(screen)
        screen_edge = cv2.Canny(screen_gray, 50, 150)
        best = None
        best_score = -1.0

        for scale in (0.65, 0.75, 0.85, 1.0, 1.15, 1.3, 1.45):
            tw = int(tpl.shape[1] * scale)
            th = int(tpl.shape[0] * scale)
            if tw < 40 or th < 40:
                continue
            if tw >= screen_gray.shape[1] or th >= screen_gray.shape[0]:
                continue
            resized = cv2.resize(tpl, (tw, th), interpolation=cv2.INTER_AREA)
            tpl_gray = self._to_gray(resized)
            tpl_edge = cv2.Canny(tpl_gray, 50, 150)

            res_gray = cv2.matchTemplate(screen_gray, tpl_gray, cv2.TM_CCOEFF_NORMED)
            _, max_gray, _, loc_gray = cv2.minMaxLoc(res_gray)

            res_edge = cv2.matchTemplate(screen_edge, tpl_edge, cv2.TM_CCOEFF_NORMED)
            _, max_edge, _, loc_edge = cv2.minMaxLoc(res_edge)

            if max_edge > max_gray:
                max_val = float(max_edge)
                max_loc = loc_edge
            else:
                max_val = float(max_gray)
                max_loc = loc_gray

            if max_val > best_score:
                best_score = max_val
                best = (max_loc[0], max_loc[1], tw, th, best_score)

        if best is not None and best[4] >= 0.28:
            return best

        # 兜底: 仅匹配卡片标题区域，对颜色/装饰变化更鲁棒。
        th0, tw0 = tpl.shape[:2]
        hx1, hx2 = int(tw0 * 0.05), int(tw0 * 0.70)
        hy1, hy2 = int(th0 * 0.05), int(th0 * 0.32)
        if hx2 - hx1 < 16 or hy2 - hy1 < 12:
            return None

        head = self._to_gray(tpl[hy1:hy2, hx1:hx2])
        best2 = None
        best2_score = -1.0

        for scale in (0.6, 0.75, 0.9, 1.05, 1.2, 1.35, 1.5):
            full_w = int(tw0 * scale)
            full_h = int(th0 * scale)
            if full_w < 50 or full_h < 50:
                continue
            if full_w >= screen_gray.shape[1] or full_h >= screen_gray.shape[0]:
                continue

            hw = int((hx2 - hx1) * scale)
            hh = int((hy2 - hy1) * scale)
            if hw < 12 or hh < 10:
                continue

            head_scaled = cv2.resize(head, (hw, hh), interpolation=cv2.INTER_AREA)
            res = cv2.matchTemplate(screen_gray, head_scaled, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            if max_val > best2_score:
                best2_score = float(max_val)
                ox = int(hx1 * scale)
                oy = int(hy1 * scale)
                x = max(0, max_loc[0] - ox)
                y = max(0, max_loc[1] - oy)
                if x + full_w > screen_gray.shape[1]:
                    x = max(0, screen_gray.shape[1] - full_w)
                if y + full_h > screen_gray.shape[0]:
                    y = max(0, screen_gray.shape[0] - full_h)
                best2 = (x, y, full_w, full_h, best2_score)

        if best2 is not None and best2[4] >= 0.22:
            return best2
        return None

    def _recognize_value_text(self, value_crop: np.ndarray, allowed_chars: Optional[str] = None) -> str:
        if value_crop is None or value_crop.size == 0:
            return ""

        # 仅识别数字相关字符；尝试两种二值化路径，选更合理结果。
        candidates: List[str] = []
        allowed = set(allowed_chars) if allowed_chars else None

        for binary in (self._binarize(value_crop), self._binarize_inv(value_crop)):
            boxes = self._find_char_boxes(binary)
            if not boxes:
                continue
            chars: List[str] = []
            for x, y, w, h in boxes:
                glyph = binary[y:y + h, x:x + w]
                if glyph.size == 0:
                    continue
                g = cv2.resize(glyph, (48, 64), interpolation=cv2.INTER_CUBIC)
                best_ch = ""
                best_score = -1.0
                for ch, samples in self.numeric_templates.items():
                    if allowed is not None and ch not in allowed:
                        continue
                    for sample in samples:
                        score = cv2.matchTemplate(g, sample, cv2.TM_CCOEFF_NORMED).max()
                        if score > best_score:
                            best_score = float(score)
                            best_ch = ch
                if best_ch and best_score > 0.18:
                    chars.append(best_ch)
            if chars:
                text = "".join(chars)
                text = re.sub(r"[^0-9\-\./%]", "", text)
                candidates.append(text)

        if not candidates:
            return ""

        # 优先选择包含更完整数字模式的结果。
        candidates.sort(key=lambda t: (len(re.findall(r"\d", t)), len(t)), reverse=True)
        return candidates[0]

    def parse_temperature(self, text: str) -> Optional[float]:
        text = text.replace("..", ".")
        m = re.search(r"-?\d{1,2}(?:\.\d)?", text)
        if m:
            try:
                return float(m.group(0))
            except Exception:
                pass

        nums = re.findall(r"\d{1,3}", text)
        if not nums:
            return None
        try:
            val = float(nums[0])
            if val > 80:
                # 例如 223 被误读为 22.3
                val = val / 10.0
            if -50 <= val <= 80:
                return val
        except Exception:
            return None
        return None

    def parse_battery(self, text: str) -> Optional[int]:
        nums = re.findall(r"\d{1,3}", text)
        if not nums:
            return None
        values = [int(x) for x in nums if 0 <= int(x) <= 100]
        if not values:
            return None
        # 百分比优先取最大值，降低前导噪声影响。
        return max(values)

    def parse_percent(self, text: str) -> Optional[int]:
        return self.parse_battery(text)

    def parse_task_progress(self, text: str) -> Optional[str]:
        m = re.search(r"(\d{1,3})\s*/\s*(\d{1,3})", text)
        if not m:
            return None
        left = int(m.group(1))
        right = int(m.group(2))
        if right <= 0:
            return None
        if left > right:
            left = min(left, right)
        return f"{left}/{right}"

    def parse_signal(self, text: str) -> Optional[int]:
        m = re.search(r"(-?\d{2,3})\s*(?:dbm|rssi)?", text, flags=re.IGNORECASE)
        if not m:
            return None
        val = int(m.group(1))
        if val > 0:
            return None
        return val

    def extract_metrics(self, screen: np.ndarray, field_map: Optional[List[OCRField]] = None) -> Dict[str, object]:
        result: Dict[str, object] = {
            "raw_text": "",
            "temperature": None,
            "battery": None,
            "signal_dbm": None,
            "ear_online_rate": None,
            "task_progress": None,
        }

        if screen is None or screen.size == 0:
            return result

        # 兼容旧配置：若显式传了 field_map，走旧按区域识别。
        if field_map:
            chunks: List[str] = []
            for field in field_map:
                x, y, w, h = field.region
                crop = screen[y:y + h, x:x + w]
                text = self._recognize_value_text(crop)
                chunks.append(f"{field.name}:{text}")
                if field.name == "temperature":
                    result["temperature"] = self.parse_temperature(text)
                elif field.name == "battery":
                    result["battery"] = self.parse_battery(text)
                elif field.name == "signal_dbm":
                    result["signal_dbm"] = self.parse_signal(text)
            result["raw_text"] = " | ".join(chunks)
            return result

        chunks: List[str] = []
        # 水温卡
        w_hit = self._match_card(screen, "water")
        if w_hit and "water" in self.card_value_rel_box:
            x, y, cw, ch, _ = w_hit
            rx1, ry1, rx2, ry2 = self.card_value_rel_box["water"]
            vx1 = x + int(cw * rx1)
            vy1 = y + int(ch * ry1)
            vx2 = x + int(cw * rx2)
            vy2 = y + int(ch * ry2)
            text = self._recognize_value_text(screen[vy1:vy2, vx1:vx2], allowed_chars="0123456789-.")
            result["temperature"] = self.parse_temperature(text)
            chunks.append(f"water:{text}")

        # 终端健康度（电量）
        h_hit = self._match_card(screen, "health")
        if h_hit and "health" in self.card_value_rel_box:
            x, y, cw, ch, _ = h_hit
            rx1, ry1, rx2, ry2 = self.card_value_rel_box["health"]
            vx1 = x + int(cw * rx1)
            vy1 = y + int(ch * ry1)
            vx2 = x + int(cw * rx2)
            vy2 = y + int(ch * ry2)
            text = self._recognize_value_text(screen[vy1:vy2, vx1:vx2], allowed_chars="0123456789%")
            result["battery"] = self.parse_battery(text)
            chunks.append(f"health:{text}")

        # 耳标在线率
        e_hit = self._match_card(screen, "ear")
        if e_hit and "ear" in self.card_value_rel_box:
            x, y, cw, ch, _ = e_hit
            rx1, ry1, rx2, ry2 = self.card_value_rel_box["ear"]
            vx1 = x + int(cw * rx1)
            vy1 = y + int(ch * ry1)
            vx2 = x + int(cw * rx2)
            vy2 = y + int(ch * ry2)
            text = self._recognize_value_text(screen[vy1:vy2, vx1:vx2], allowed_chars="0123456789%")
            result["ear_online_rate"] = self.parse_percent(text)
            chunks.append(f"ear:{text}")

        # 巡检任务
        t_hit = self._match_card(screen, "task")
        if t_hit and "task" in self.card_value_rel_box:
            x, y, cw, ch, _ = t_hit
            rx1, ry1, rx2, ry2 = self.card_value_rel_box["task"]
            vx1 = x + int(cw * rx1)
            vy1 = y + int(ch * ry1)
            vx2 = x + int(cw * rx2)
            vy2 = y + int(ch * ry2)
            text = self._recognize_value_text(screen[vy1:vy2, vx1:vx2], allowed_chars="0123456789/")
            result["task_progress"] = self.parse_task_progress(text)
            chunks.append(f"task:{text}")

        # 信号值：尝试在健康卡下半区做一次快速匹配（保守策略）
        if h_hit:
            x, y, cw, ch, _ = h_hit
            sub = screen[y + int(ch * 0.58): y + int(ch * 0.92), x + int(cw * 0.10): x + int(cw * 0.90)]
            sub_text = self._recognize_value_text(sub, allowed_chars="-0123456789")
            sig = self.parse_signal(sub_text)
            if sig is not None:
                result["signal_dbm"] = sig
            if sub_text:
                chunks.append(f"signal:{sub_text}")

        result["raw_text"] = " | ".join(chunks)
        return result

    def extract_metrics_from_file(self, image_path: str, field_map: Optional[List[OCRField]] = None) -> Dict[str, object]:
        image = self._read_image(image_path)
        if image is None:
            return {
                "raw_text": "",
                "temperature": None,
                "battery": None,
                "signal_dbm": None,
                "ear_online_rate": None,
                "task_progress": None,
            }
        return self.extract_metrics(image, field_map=field_map)
