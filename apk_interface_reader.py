from __future__ import annotations

import os
import re
import zipfile
from typing import Dict, List, Set


_ASCII_PATTERN = re.compile(rb"[\x20-\x7E]{4,}")
_URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_WS_PATTERN = re.compile(r"wss?://[^\s\"'<>]+", re.IGNORECASE)
_URI_PATTERN = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s\"'<>]+")
_PACKAGE_PATTERN = re.compile(r"[a-zA-Z][\w]*(?:\.[a-zA-Z][\w]*){2,}")
_ACTIVITY_PATTERN = re.compile(r"[a-zA-Z][\w]*(?:\.[A-Za-z][\w$]*)+")


def _is_probable_launch_activity(name: str) -> bool:
    if not name or "." not in name:
        return False
    lower = name.lower()
    blocked = ("permission", "provider", "receiver", "service")
    if any(k in lower for k in blocked):
        return False
    if lower.endswith("activity"):
        return True
    if ".activity" in lower or "mainactivity" in lower:
        return True
    return False


def _extract_ascii_strings(blob: bytes, limit: int = 20000) -> List[str]:
    out: List[str] = []
    for m in _ASCII_PATTERN.finditer(blob):
        try:
            s = m.group(0).decode("utf-8", errors="ignore").strip()
        except Exception:
            continue
        if len(s) >= 4:
            out.append(s)
            if len(out) >= limit:
                break
    return out


def _pick_package(candidates: Set[str], activities: Set[str]) -> tuple[str, List[str], float]:
    # 过滤常见系统包，优先 com./cn./net./org.
    filtered = [
        c
        for c in candidates
        if not c.startswith(("android.", "java.", "kotlin.", "javax.", "dalvik.", "okhttp3."))
    ]
    preferred = [c for c in filtered if c.startswith(("com.", "cn.", "net.", "org."))]
    pool = preferred or filtered
    if not pool:
        return "", [], 0.0

    scored = []
    for pkg in pool:
        score = 0.0
        segs = pkg.split(".")
        if 3 <= len(segs) <= 6:
            score += 0.4
        if pkg.startswith(("com.", "cn.", "net.", "org.")):
            score += 0.3
        if any(a.startswith(pkg + ".") for a in activities):
            score += 0.5
        if "test" in pkg.lower() or "debug" in pkg.lower():
            score -= 0.2
        scored.append((pkg, score))

    scored.sort(key=lambda item: item[1], reverse=True)
    best_pkg, best_score = scored[0]
    top_candidates = [pkg for pkg, _ in scored[:8]]
    return best_pkg, top_candidates, max(0.0, min(1.0, best_score))


def analyze_apk_interfaces(apk_path: str) -> Dict[str, object]:
    if not os.path.isfile(apk_path):
        return {"ok": False, "error": f"APK not found: {apk_path}"}

    urls: Set[str] = set()
    ws_urls: Set[str] = set()
    uris: Set[str] = set()
    package_candidates: Set[str] = set()
    activity_candidates: Set[str] = set()
    deeplink_schemes: Set[str] = set()

    try:
        with zipfile.ZipFile(apk_path, "r") as zf:
            names = zf.namelist()
            manifest_data = b""
            if "AndroidManifest.xml" in names:
                manifest_data = zf.read("AndroidManifest.xml")
                for s in _extract_ascii_strings(manifest_data, limit=6000):
                    for u in _URL_PATTERN.findall(s):
                        urls.add(u)
                    for u in _WS_PATTERN.findall(s):
                        ws_urls.add(u)
                    for u in _URI_PATTERN.findall(s):
                        uris.add(u)
                    for p in _PACKAGE_PATTERN.findall(s):
                        package_candidates.add(p)
                    for a in _ACTIVITY_PATTERN.findall(s):
                        if "." in a and not a.startswith(("android.", "java.")):
                            activity_candidates.add(a)
                try:
                    manifest_u16 = manifest_data.decode("utf-16le", errors="ignore")
                    for p in _PACKAGE_PATTERN.findall(manifest_u16):
                        package_candidates.add(p)
                    for a in _ACTIVITY_PATTERN.findall(manifest_u16):
                        if "." in a and not a.startswith(("android.", "java.")):
                            activity_candidates.add(a)
                except Exception:
                    pass

            for name in names:
                if not name.endswith(".dex"):
                    continue
                try:
                    dex = zf.read(name)
                except Exception:
                    continue
                for s in _extract_ascii_strings(dex):
                    for u in _URL_PATTERN.findall(s):
                        urls.add(u)
                    for u in _WS_PATTERN.findall(s):
                        ws_urls.add(u)
                    for u in _URI_PATTERN.findall(s):
                        uris.add(u)
                    for p in _PACKAGE_PATTERN.findall(s):
                        package_candidates.add(p)
                    if "://" in s:
                        scheme = s.split("://", 1)[0].lower().strip()
                        if 1 <= len(scheme) <= 24 and scheme.isascii() and scheme.replace("+", "").replace("-", "").isalnum():
                            deeplink_schemes.add(scheme)
                    if s.endswith("Activity") and "." in s:
                        activity_candidates.add(s)

    except zipfile.BadZipFile:
        return {"ok": False, "error": "Invalid APK/ZIP file"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    package_name, package_candidates_top, package_confidence = _pick_package(package_candidates, activity_candidates)
    launch_activity = ""
    launch_candidates: List[str] = []
    if package_name:
        same_pkg_activities = sorted([a for a in activity_candidates if a.startswith(package_name + ".")])
        if same_pkg_activities:
            preferred = [a for a in same_pkg_activities if _is_probable_launch_activity(a)]
            launch_candidates = preferred or same_pkg_activities
            launch_activity = launch_candidates[0]

    # 精简输出数量，避免 UI 过载。
    urls_out = sorted(urls)[:30]
    ws_out = sorted(ws_urls)[:20]
    uri_out = sorted([u for u in uris if u not in urls and u not in ws_urls])[:30]

    return {
        "ok": True,
        "apk_path": apk_path,
        "package_name": package_name,
        "package_confidence": package_confidence,
        "package_candidates": package_candidates_top,
        "launch_activity": launch_activity,
        "launch_activity_candidates": launch_candidates[:8],
        "http_endpoints": urls_out,
        "ws_endpoints": ws_out,
        "uri_schemes": sorted(deeplink_schemes)[:20],
        "other_uris": uri_out,
        "candidate_activity_count": len(activity_candidates),
    }
