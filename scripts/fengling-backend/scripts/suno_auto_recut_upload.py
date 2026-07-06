#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Suno Studio automatic recut -> upload chunks -> same-track assemble -> render.

Designed for Windows + the user's logged-in Chrome/Edge.
- Windows uses ffmpeg to decode to WAV (bin/ffmpeg.exe bundled).
- Splits into <=5.5s WAV chunks at low-energy points, with tiny overlaps.
- Uploads chunks to Suno via the live Chrome/Edge Clerk session (CDP).
- Writes all chunks to a new Studio project with crossfades, then calls render-state.
"""
from __future__ import annotations

# ── Auto-install missing dependencies ─────────────────────────────────────────
def _ensure_deps():
    import subprocess, sys
    pkgs = [
        ("numpy",     "numpy"),
        ("requests",  "requests"),
        ("websocket", "websocket-client"),
    ]
    missing = []
    for mod, pip_name in pkgs:
        try:
            __import__(mod)
        except ImportError:
            missing.append(pip_name)
    if not missing:
        return
    print("[setup] Auto-installing: " + str(missing), flush=True)
    for extra in (["--user"], ["--break-system-packages"], []):
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "--quiet"] + extra + missing,
                stderr=subprocess.DEVNULL
            )
            print("[setup] Done.", flush=True)
            return
        except subprocess.CalledProcessError:
            continue
    print("[setup] Warning: could not auto-install. Please run: pip install " + " ".join(missing), flush=True)

_ensure_deps()
# ──────────────────────────────────────────────────────────────────────────────

import argparse
import base64
import concurrent.futures as cf
import copy
import json
import mimetypes
import os
import pathlib
import re
import shutil
import subprocess
import threading
import sys
import time
import urllib.parse
import uuid
import wave
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import numpy as np
import requests

# ── path bootstrap ─────────────────────────────────────────────────────────────
_SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
_candidate_import_paths = [_SCRIPT_DIR, pathlib.Path.cwd() / "scripts"]
_app_root_env = os.environ.get("SUNO_APP_ROOT", "").strip()
if _app_root_env:
    _candidate_import_paths.append(pathlib.Path(_app_root_env).expanduser().resolve() / "scripts")
for _p in _candidate_import_paths:
    try:
        if _p.exists() and str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
    except Exception:
        pass

# ── constants ──────────────────────────────────────────────────────────────────
API        = "https://studio-api-prod.suno.com"
CHROME_APP = os.environ.get("SUNO_CHROME_APP", "")
_ROOT_FROM_SCRIPT = pathlib.Path(__file__).resolve().parents[1]
_ROOT_FROM_ENV = os.environ.get("SUNO_APP_ROOT", "")
ROOT = pathlib.Path(_ROOT_FROM_ENV) if _ROOT_FROM_ENV else _ROOT_FROM_SCRIPT
try:
    if not ROOT.exists():
        ROOT = _ROOT_FROM_SCRIPT
except OSError:
    ROOT = _ROOT_FROM_SCRIPT
IS_WINDOWS = sys.platform.startswith("win")
IS_DARWIN  = sys.platform == "darwin"

DEFAULT_TOKEN_FILE = ROOT / "evidence/network/current_browser_token.json"
CDP_PORT           = int(os.environ.get("SUNO_CDP_PORT", "23922"))
BROWSER_PROFILE    = pathlib.Path(
    os.environ.get("SUNO_BROWSER_PROFILE", ROOT / "browser-profile")
).expanduser()

# Windows and the migrated macOS CLI use a dedicated CDP browser window.
USE_CDP_BROWSER    = os.environ.get("SUNO_USE_CDP_BROWSER", "1" if (IS_WINDOWS or IS_DARWIN) else "0") == "1"
DEFAULT_BROWSER_ONLY = os.environ.get("SUNO_DEFAULT_BROWSER_ONLY", "0" if (IS_WINDOWS or IS_DARWIN) else "1") != "0"
USE_OPENCLI_BROWSER = os.environ.get("SUNO_USE_OPENCLI_BROWSER", "1" if IS_DARWIN else "0") == "1"
OPENCLI_SESSION     = os.environ.get("SUNO_OPENCLI_SESSION", "fengling-suno")

# ── subprocess helper ──────────────────────────────────────────────────────────

def sh(cmd: list[str], **kw):
    return subprocess.run(cmd, check=True, text=True, capture_output=True, **kw)


OPENCLI_EXPORT_JS = (
    "(async()=>JSON.stringify({"
    "token:await window.Clerk?.session?.getToken?.()||'',"
    "tokenSource:(await window.Clerk?.session?.getToken?.())?'opencli-clerk':'',"
    "tokenMeta:{clerk:!!window.Clerk,session:!!window.Clerk?.session,ts:Date.now()},"
    "deviceId:localStorage.getItem('ajs_anonymous_id')||'',"
    "projectKeys:Object.keys(localStorage).filter(k=>k.includes('studio-project-management-project-info'))"
    ".map(k=>[k,localStorage.getItem(k)]),"
    "href:location.href,ts:Date.now()}))()"
)


def export_opencli_suno_token(
    token_file: pathlib.Path = DEFAULT_TOKEN_FILE,
    require_project_keys: bool = False,
    login_url: str = "https://suno.com/",
) -> Optional[Dict[str, Any]]:
    if not USE_OPENCLI_BROWSER or not shutil.which("opencli"):
        return None
    session = OPENCLI_SESSION
    try:
        subprocess.run(
            ["opencli", "browser", session, "open", login_url, "--window", "foreground"],
            text=True, capture_output=True, timeout=45, check=True,
        )
        raw = subprocess.run(
            ["opencli", "browser", session, "eval", OPENCLI_EXPORT_JS],
            text=True, capture_output=True, timeout=30, check=True,
        ).stdout.strip()
        if "\n\n  Update available:" in raw:
            raw = raw.split("\n\n  Update available:", 1)[0].strip()
        data = json.loads(raw)
        if not data.get("token"):
            return None
        if require_project_keys and not data.get("projectKeys"):
            subprocess.run(
                ["opencli", "browser", session, "open", "https://suno.com/studio", "--window", "foreground"],
                text=True, capture_output=True, timeout=45, check=True,
            )
            raw = subprocess.run(
                ["opencli", "browser", session, "eval", OPENCLI_EXPORT_JS],
                text=True, capture_output=True, timeout=30, check=True,
            ).stdout.strip()
            if "\n\n  Update available:" in raw:
                raw = raw.split("\n\n  Update available:", 1)[0].strip()
            data = json.loads(raw)
        if require_project_keys and not data.get("projectKeys"):
            return None
        data["tokenSource"] = data.get("tokenSource") or "opencli-clerk"
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[auth] Suno login OK tokenSource={data.get('tokenSource')} href={data.get('href')}",
              flush=True)
        return data
    except Exception as e:
        print(f"[auth] OpenCLI login reuse unavailable, falling back to dedicated browser: {e}",
              flush=True)
        return None


# ── macOS AppleScript (unused on Windows but kept for completeness) ────────────

def run_osascript(script: str) -> str:
    if not IS_DARWIN:
        raise RuntimeError("当前系统不是 macOS，不能使用 AppleScript；请安装 Chrome 或 Microsoft Edge。")
    p = subprocess.run(["osascript", "-e", script], text=True, capture_output=True)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or f"osascript exit {p.returncode}").strip())
    return p.stdout.strip()


# ── CDP state ─────────────────────────────────────────────────────────────────
_CDP_DISABLED  = False
_CDP_PAGE_ID: Optional[str] = None


def cdp_base() -> str:
    return f"http://127.0.0.1:{CDP_PORT}"


def cdp_alive() -> bool:
    if not USE_CDP_BROWSER:
        return False
    try:
        r = requests.get(cdp_base() + "/json/version", timeout=1.5)
        return r.ok
    except Exception:
        return False


def cdp_version() -> Dict[str, Any]:
    try:
        r = requests.get(cdp_base() + "/json/version", timeout=3)
        r.raise_for_status()
        return r.json() if r.text else {}
    except Exception:
        return {}


def cdp_request(path: str, method: str = "GET") -> Any:
    url = cdp_base() + path
    fn  = requests.put if method.upper() == "PUT" else requests.get
    r   = fn(url, timeout=5)
    r.raise_for_status()
    return r.json() if r.text else None


def cdp_pages() -> List[Dict[str, Any]]:
    try:
        pages = cdp_request("/json/list")
        return pages if isinstance(pages, list) else []
    except Exception:
        return []


def cdp_new_page(url: str) -> Dict[str, Any]:
    q    = urllib.parse.quote(url, safe="")
    last = None
    for method in ("PUT", "GET"):
        try:
            p = cdp_request("/json/new?" + q, method=method)
            if isinstance(p, dict):
                return p
        except Exception as e:
            last = e
    raise RuntimeError(f"CDP 新建页面失败: {last}")


def cdp_pick_page(url: str = "https://suno.com/") -> Dict[str, Any]:
    global _CDP_PAGE_ID
    pages = [p for p in cdp_pages()
             if p.get("type") == "page" and p.get("webSocketDebuggerUrl")]
    if _CDP_PAGE_ID:
        for p in pages:
            if p.get("id") == _CDP_PAGE_ID and "suno.com" in (p.get("url") or ""):
                return p
    for p in pages:
        if "suno.com" in (p.get("url") or ""):
            _CDP_PAGE_ID = p.get("id")
            return p
    for p in pages:
        page_url = (p.get("url") or "").lower()
        if page_url in {"about:blank", "chrome://newtab/", ""}:
            _CDP_PAGE_ID = p.get("id")
            try:
                pid = p.get("id")
                if pid:
                    cdp_request("/json/activate/" + urllib.parse.quote(str(pid), safe=""))
            except Exception:
                pass
            try:
                cdp_call(p["webSocketDebuggerUrl"], "Page.enable")
            except Exception:
                pass
            cdp_call(p["webSocketDebuggerUrl"], "Page.navigate", {"url": url}, timeout_sec=45)
            return p
    p = cdp_new_page(url)
    _CDP_PAGE_ID = p.get("id")
    return p


def cdp_call(ws_url: str, method: str,
             params: Optional[Dict[str, Any]] = None,
             timeout_sec: int = 60) -> Any:
    import websocket
    ws = websocket.create_connection(ws_url, timeout=max(10, min(timeout_sec, 60)))
    try:
        try:
            ws.settimeout(max(10, min(timeout_sec, 60)))
        except Exception:
            pass
        msg_id = int(time.time() * 1000) % 100_000_000
        ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}},
                            separators=(",", ":")))
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            msg = json.loads(ws.recv())
            if msg.get("id") == msg_id:
                if msg.get("error"):
                    raise RuntimeError(f"CDP {method} error: {msg['error']}")
                return msg.get("result")
        raise RuntimeError(f"CDP {method} timeout after {timeout_sec}s")
    finally:
        try:
            ws.close()
        except Exception:
            pass


def cdp_navigate(url: str) -> None:
    if not launch_cdp_browser(url):
        raise RuntimeError("CDP browser unavailable")
    p  = cdp_pick_page(url)
    ws = p["webSocketDebuggerUrl"]
    try:
        cdp_call(ws, "Page.enable")
    except Exception:
        pass
    cdp_call(ws, "Page.navigate", {"url": url}, timeout_sec=45)


def cdp_eval(js: str) -> str:
    global _CDP_PAGE_ID
    last_error = ""
    for attempt in range(3):
        if not launch_cdp_browser("https://suno.com/"):
            raise RuntimeError("CDP browser unavailable")
        try:
            p = cdp_pick_page("https://suno.com/")
            if "suno.com" not in (p.get("url") or ""):
                cdp_navigate("https://suno.com/")
                time.sleep(2)
                p = cdp_pick_page("https://suno.com/")
            res = cdp_call(p["webSocketDebuggerUrl"], "Runtime.evaluate", {
                "expression":   js,
                "awaitPromise": True,
                "returnByValue": True,
                "userGesture":  True,
                "timeout":      45000,
            }, timeout_sec=65)
            if isinstance(res, dict) and res.get("exceptionDetails"):
                raise RuntimeError("CDP JS exception: " +
                                   json.dumps(res.get("exceptionDetails"),
                                              ensure_ascii=False)[:500])
            rr = (res or {}).get("result") or {}
            if "value" in rr:
                return str(rr.get("value") or "")
            return str(rr.get("description") or "")
        except Exception as e:
            last_error = str(e)
            _CDP_PAGE_ID = None
            print(f"[auth-reconnect] browser channel timeout/retry {attempt + 1}/3: {last_error}",
                  flush=True)
            try:
                cdp_navigate("https://suno.com/")
            except Exception:
                pass
            time.sleep(2 + attempt * 2)
    raise RuntimeError(last_error or "CDP Runtime.evaluate failed")


# ── browser search ─────────────────────────────────────────────────────────────

def browser_candidates() -> List[str]:
    home = pathlib.Path.home()
    vals: List[str] = []
    bundled = ROOT / "browser-bin" / "chromium" / "chrome.exe"
    if IS_WINDOWS:
        pf    = os.environ.get("ProgramFiles",        r"C:\Program Files")
        pfx86 = os.environ.get("ProgramFiles(x86)",   r"C:\Program Files (x86)")
        local = os.environ.get("LocalAppData",
                               str(home / "AppData" / "Local"))
        vals += [
            str(pathlib.Path(pf)    / "Microsoft" / "Edge"    / "Application" / "msedge.exe"),
            str(pathlib.Path(pfx86) / "Microsoft" / "Edge"    / "Application" / "msedge.exe"),
            str(pathlib.Path(local) / "Microsoft" / "Edge"    / "Application" / "msedge.exe"),
            "msedge",
            str(pathlib.Path(pf)    / "Google"    / "Chrome"  / "Application" / "chrome.exe"),
            str(pathlib.Path(pfx86) / "Google"    / "Chrome"  / "Application" / "chrome.exe"),
            str(pathlib.Path(local) / "Google"    / "Chrome"  / "Application" / "chrome.exe"),
            "chrome",
        ]
        if CHROME_APP:
            vals.append(CHROME_APP)
        vals += [str(bundled), "chromium"]
    else:
        vals += [
            str(home / "Desktop" / "Microsoft Edge.app"),
            "/Applications/Microsoft Edge.app",
            str(home / "Applications" / "Microsoft Edge.app"),
            "Microsoft Edge",
            str(home / "Desktop" / "Google Chrome.app"),
            "/Applications/Google Chrome.app",
            str(home / "Applications" / "Google Chrome.app"),
            "Google Chrome",
            str(home / "Desktop" / "Chromium.app"),
            "/Applications/Chromium.app",
            str(home / "Applications" / "Chromium.app"),
            "Chromium",
        ]
        if CHROME_APP:
            vals.append(CHROME_APP)
        vals.append(str(bundled))
    out: List[str] = []
    seen = set()
    for v in vals:
        v = str(v).strip()
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def mac_app_bundle_executable(app: pathlib.Path) -> Optional[pathlib.Path]:
    names = {
        "Google Chrome.app": "Google Chrome",
        "Microsoft Edge.app": "Microsoft Edge",
        "Chromium.app": "Chromium",
    }
    exe_name = names.get(app.name)
    if not exe_name:
        return None
    exe = app / "Contents" / "MacOS" / exe_name
    if exe.exists():
        return exe
    return None


def resolve_browser_candidate(candidate: str) -> Optional[pathlib.Path]:
    cp = pathlib.Path(candidate)
    if IS_WINDOWS:
        if candidate.lower().endswith(".exe") and cp.exists():
            return cp
        found = shutil.which(candidate)
        return pathlib.Path(found) if found else None
    if IS_DARWIN and cp.suffix == ".app" and cp.exists():
        return mac_app_bundle_executable(cp)
    if cp.exists() and os.access(cp, os.X_OK):
        return cp
    found = shutil.which(candidate)
    return pathlib.Path(found) if found else None


def find_browser_binary() -> Optional[pathlib.Path]:
    for c in browser_candidates():
        p = resolve_browser_candidate(c)
        if p:
            return p
    return None


def find_browser_binaries() -> List[pathlib.Path]:
    out: List[pathlib.Path] = []
    seen = set()
    for c in browser_candidates():
        p = resolve_browser_candidate(c)
        if p:
            key = str(p).lower()
            if key not in seen:
                seen.add(key)
                out.append(p)
    return out


def stop_profile_browser_processes() -> None:
    if not IS_WINDOWS:
        return
    try:
        prof = str(BROWSER_PROFILE)
        subprocess.run([
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
            "$p=$env:SUNO_BROWSER_PROFILE; "
            "Get-CimInstance Win32_Process | "
            "Where-Object { ($_.Name -match '^(chrome|msedge|chromium)\\.exe$') "
            "-and ($_.CommandLine -like ('*--user-data-dir=' + $p + '*')) } | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
        ], text=True, capture_output=True, timeout=10,
           env={**os.environ, "SUNO_BROWSER_PROFILE": prof})
    except Exception:
        pass


def write_browser_diagnostic(**data: Any) -> None:
    try:
        path = ROOT / "evidence" / "network" / "browser_diagnostic.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "ts": int(time.time()),
            "cdp_port": CDP_PORT,
            "browser_profile": str(BROWSER_PROFILE),
            "cdp_alive": cdp_alive(),
        }
        payload.update(data)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def launch_cdp_browser(url: str = "https://suno.com/") -> bool:
    global _CDP_DISABLED
    if _CDP_DISABLED or not USE_CDP_BROWSER:
        return False
    if cdp_alive():
        return True
    reuse_existing = os.environ.get("SUNO_REUSE_LOGIN_BROWSER", "0") == "1"
    preheat_mode = os.environ.get("SUNO_PREHEAT_BROWSER", "0") == "1"
    if reuse_existing or preheat_mode:
        wait_until = time.time() + 8
        while time.time() < wait_until:
            if cdp_alive():
                return True
            time.sleep(0.2)
    browsers = find_browser_binaries()
    write_browser_diagnostic(stage="before_launch", candidates=[str(p) for p in browsers])
    if not browsers:
        _CDP_DISABLED = True
        print("[auth] 没找到 Chrome/Edge/Chromium 浏览器；请先安装 Chrome 或 Microsoft Edge。",
              flush=True)
        write_browser_diagnostic(stage="no_browser", candidates=[])
        return False
    BROWSER_PROFILE.mkdir(parents=True, exist_ok=True)
    safe_url = url or "https://suno.com/"
    last_error = ""
    for exe in browsers:
        if not (reuse_existing or preheat_mode):
            stop_profile_browser_processes()
            time.sleep(0.5)
        args = [
            str(exe),
            f"--remote-debugging-port={CDP_PORT}",
            "--remote-allow-origins=*",
            f"--user-data-dir={BROWSER_PROFILE}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate",
            "--disable-extensions",
            "--disable-session-crashed-bubble",
            "--new-window",
            safe_url,
        ]
        if preheat_mode:
            args.insert(-2, "--start-minimized")
        try:
            subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(f"[auth] 正在启动专用浏览器: {exe}", flush=True)
            write_browser_diagnostic(stage="launched", browser=str(exe), args=args)
        except Exception as e:
            last_error = f"{exe}: {e}"
            write_browser_diagnostic(stage="launch_failed", browser=str(exe), error=str(e))
            continue
        deadline = time.time() + 45
        while time.time() < deadline:
            if cdp_alive():
                if not preheat_mode:
                    try:
                        pages = cdp_pages()
                        if not any("suno.com" in (p.get("url") or "") for p in pages):
                            cdp_pick_page(safe_url)
                    except Exception:
                        pass
                print(f"[auth] 专用浏览器已打开（端口 {CDP_PORT}，登录数据 {BROWSER_PROFILE}）",
                      flush=True)
                write_browser_diagnostic(stage="connected", browser=str(exe))
                return True
            time.sleep(0.5)
        last_error = f"{exe}: CDP 连接超时"
        write_browser_diagnostic(stage="timeout", browser=str(exe), error=last_error)
    _CDP_DISABLED = True
    print(f"[auth] 专用浏览器 CDP 连接失败；请关闭浏览器后重试，或安装 Chrome/Edge。最后错误: {last_error}",
          flush=True)
    write_browser_diagnostic(stage="failed", error=last_error, candidates=[str(p) for p in browsers])
    return False


def reset_cdp_browser() -> None:
    global _CDP_DISABLED, _CDP_PAGE_ID
    _CDP_DISABLED = False
    _CDP_PAGE_ID  = None
    if not USE_CDP_BROWSER:
        return
    try:
        close_cdp_browser()
        time.sleep(1.0)
    except Exception:
        pass
    if IS_WINDOWS:
        try:
            prof = str(BROWSER_PROFILE)
            subprocess.run([
                "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                "$p=$env:SUNO_BROWSER_PROFILE; "
                "Get-CimInstance Win32_Process | "
                "Where-Object { ($_.Name -match '^(chrome|msedge|chromium)\\.exe$') "
                "-and ($_.CommandLine -like ('*--user-data-dir=' + $p + '*')) } | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
            ], text=True, capture_output=True, timeout=10,
               env={**os.environ, "SUNO_BROWSER_PROFILE": prof})
        except Exception:
            pass
        time.sleep(1.0)


def close_cdp_browser() -> None:
    global _CDP_PAGE_ID
    if not USE_CDP_BROWSER:
        return
    ws_url = (cdp_version() or {}).get("webSocketDebuggerUrl")
    if not ws_url:
        pages  = cdp_pages()
        ws_url = next((p.get("webSocketDebuggerUrl") for p in pages
                       if p.get("webSocketDebuggerUrl")), None)
    if ws_url:
        try:
            cdp_call(ws_url, "Browser.close", timeout_sec=8)
            print("[auth] login browser closed", flush=True)
        except Exception as e:
            print(f"[auth] login browser close skipped: {e}", flush=True)
    _CDP_PAGE_ID = None


# ── token capture ─────────────────────────────────────────────────────────────

def load_saved_login(token_file: pathlib.Path = DEFAULT_TOKEN_FILE,
                     max_age_sec: int = 7200) -> Optional[Dict[str, Any]]:
    try:
        if not token_file.exists():
            return None
        data = json.loads(token_file.read_text(encoding="utf-8-sig"))
        if not data.get("token"):
            return None
        href = str(data.get("href") or "")
        source = str(data.get("tokenSource") or "")
        if source == "storage-jwt" or "__clerk_handshake" in href:
            try:
                token_file.unlink()
            except Exception:
                pass
            return None
        ts = float(data.get("ts") or 0) / 1000.0
        if ts and time.time() - ts > max_age_sec:
            return None
        return data
    except Exception:
        return None


def clear_saved_login_token(reason: str = "") -> None:
    try:
        if DEFAULT_TOKEN_FILE.exists():
            DEFAULT_TOKEN_FILE.unlink()
            if reason:
                print(f"[auth] cleared saved Suno login token: {reason}", flush=True)
    except Exception as e:
        print(f"[auth] saved token cleanup skipped: {e}", flush=True)


def wait_for_refreshed_extension_login(
    previous_token: str,
    timeout_sec: float = 25.0,
    token_file: pathlib.Path = DEFAULT_TOKEN_FILE,
) -> Optional[Dict[str, Any]]:
    started_at_ms = now_ms()
    deadline = time.time() + max(1.0, timeout_sec)
    last_error = ""
    while time.time() < deadline:
        try:
            data = json.loads(token_file.read_text(encoding="utf-8-sig"))
            token = str(data.get("token") or "")
            source = str(data.get("tokenSource") or "")
            received_at = int(data.get("receivedAt") or data.get("ts") or 0)
            freshly_reported = received_at >= started_at_ms - 1000
            changed = token != previous_token
            if token and source.startswith("fengling-extension") and (changed or freshly_reported):
                try:
                    probe = requests.get(
                        API + "/api/feed/v2?is_liked=false&limit=1&page=0",
                        headers={
                            "Authorization": "Bearer " + token,
                            "Origin": "https://suno.com",
                            "Referer": "https://suno.com/",
                            "User-Agent": (
                                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/136.0.0.0 Safari/537.36"
                            ),
                        },
                        timeout=8,
                    )
                    if probe.ok:
                        state = "new token" if changed else "renewed session"
                        print(f"[auth-refresh] extension {state} verified source={source}",
                              flush=True)
                        return data
                    last_error = f"extension token probe returned {probe.status_code}"
                except requests.RequestException as e:
                    last_error = f"extension token probe failed: {e}"
        except FileNotFoundError:
            pass
        except Exception as e:
            last_error = str(e)
        time.sleep(0.25)
    if last_error:
        print(f"[auth-refresh] extension token wait ended: {last_error}", flush=True)
    return None


def wait_for_suno_page(target_url: str = "https://suno.com/",
                       match: str = "suno.com", timeout: int = 45):
    open_url_in_browser(target_url)
    time.sleep(2)
    try:
        browser_navigate(target_url)
    except Exception as e:
        print(f"[auth] 已尝试打开浏览器，等待页面: {e}", flush=True)
    deadline   = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        try:
            href = browser_eval_js("location.href")
            if match in href:
                return
        except Exception as e:
            last_error = str(e)
        time.sleep(1)
    raise RuntimeError(
        f"没有成功连接 Suno 登录页 {target_url}；请确认已安装 Chrome 或 Microsoft Edge，"
        f"并用本工具打开的浏览器窗口完成登录。最后错误: {last_error}"
    )


def browser_navigate(url: str) -> None:
    if launch_cdp_browser(url):
        cdp_navigate(url)
        return
    raise RuntimeError("没有连接到 Chrome/Edge 登录窗口；请安装 Chrome 或 Microsoft Edge 后重试。")


def browser_eval_js(js: str) -> str:
    if launch_cdp_browser("https://suno.com/"):
        return cdp_eval(js)
    raise RuntimeError("没有连接到 Chrome/Edge 登录窗口；请安装 Chrome 或 Microsoft Edge 后重试。")


def browser_cookie_token() -> Dict[str, Any]:
    if not launch_cdp_browser("https://suno.com/"):
        return {}
    try:
        p = cdp_pick_page("https://suno.com/")
        ws = p["webSocketDebuggerUrl"]
        try:
            cdp_call(ws, "Network.enable", timeout_sec=8)
        except Exception:
            pass
        res = cdp_call(ws, "Network.getCookies", {
            "urls": [
                "https://suno.com/",
                "https://www.suno.com/",
                "https://clerk.suno.com/",
                "https://accounts.suno.com/",
            ]
        }, timeout_sec=10)
        cookies = res.get("cookies", []) if isinstance(res, dict) else []
        names = [str(c.get("name") or "") for c in cookies if isinstance(c, dict)]
        for c in cookies:
            if not isinstance(c, dict):
                continue
            name = str(c.get("name") or "")
            value = str(c.get("value") or "")
            if value and (name == "__session" or name.startswith("__session")):
                return {"token": value, "tokenSource": "cdp-cookie", "cookieNames": names}
        return {"cookieNames": names}
    except Exception as e:
        return {"cookieError": str(e)}


def open_url_in_browser(url: str) -> None:
    if USE_CDP_BROWSER:
        if launch_cdp_browser(url):
            cdp_navigate(url)
            return
    if IS_WINDOWS:
        os.startfile(url)  # type: ignore[attr-defined]
        raise RuntimeError("Windows 版需要 Chrome 或 Microsoft Edge 的专用登录窗口，请安装后重试。")
    subprocess.run(["open" if IS_DARWIN else "xdg-open", url],
                   check=True, text=True, capture_output=True, timeout=20)


def export_chrome_suno_token(
    token_file: pathlib.Path = DEFAULT_TOKEN_FILE,
    login_timeout: int = 600,
    require_project_keys: bool = False,
    login_url: str = "https://suno.com/",
    close_after_success: bool = False,
) -> Dict[str, Any]:
    token_file.parent.mkdir(parents=True, exist_ok=True)
    opencli_ctx = export_opencli_suno_token(token_file, require_project_keys, login_url)
    if opencli_ctx:
        return opencli_ctx
    wait_for_suno_page(login_url, "suno.com")
    deadline   = time.time() + max(10, login_timeout)
    last_error = ""
    last_prompt = 0.0
    confirm_file = pathlib.Path(os.environ.get("SUNO_LOGIN_CONFIRM_FILE", "")).expanduser() if os.environ.get("SUNO_LOGIN_CONFIRM_FILE") else None
    rejected_token = ""
    waiting_logout_after_reject = False

    cdp_export_js = (
        "(async()=>{"
        "const cookie=Object.fromEntries(document.cookie.split(/;\\s*/).filter(Boolean)"
        ".map(x=>{const i=x.indexOf('=');return [x.slice(0,i),decodeURIComponent(x.slice(i+1))]}));"
        "let clerkToken='';let clerkMeta={clerk:!!window.Clerk,session:!!window.Clerk?.session,ts:Date.now()};"
        "try{clerkToken=await window.Clerk?.session?.getToken?.()||'';clerkMeta.len=clerkToken?String(clerkToken).length:0}"
        "catch(e){clerkMeta.error=String(e)};"
        "let meta={};try{meta=JSON.parse(localStorage.getItem('codex_suno_token_meta')||'{}')}catch(e){};"
        "const stored=localStorage.getItem('codex_suno_token')||'';"
        "const freshStored=stored&&meta.ts&&(Date.now()-meta.ts<300000)&&!meta.error?stored:'';"
        "const cookieToken=cookie.__session||Object.entries(cookie).find(([k])=>k.startsWith('__session'))?.[1]||'';"
        "return JSON.stringify({"
        "token:clerkToken||cookieToken||freshStored,"
        "tokenSource:clerkToken?'clerk':(cookieToken?'cookie':(freshStored?'localStorage-fresh':'')),"
        "tokenMeta:clerkMeta,"
        "deviceId:localStorage.getItem('ajs_anonymous_id')||'',"
        "projectKeys:Object.keys(localStorage).filter(k=>k.includes('studio-project-management-project-info'))"
        ".map(k=>[k,localStorage.getItem(k)]),"
        "href:location.href,ts:Date.now()})})();"
    )

    while time.time() < deadline:
        try:
            raw  = browser_eval_js(cdp_export_js)
            data = json.loads(raw)
            if not data.get("token"):
                cookie_data = browser_cookie_token()
                data.update({k: v for k, v in cookie_data.items() if k not in data or not data.get(k)})
                if cookie_data.get("token"):
                    data["token"] = cookie_data["token"]
                    data["tokenSource"] = cookie_data.get("tokenSource") or "cdp-cookie"
            if not data.get("token"):
                rejected_token = ""
                waiting_logout_after_reject = False
            if data.get("token") and (data.get("projectKeys") or not require_project_keys):
                current_token = str(data.get("token") or "")
                if confirm_file and waiting_logout_after_reject and current_token == rejected_token:
                    last_error = "当前已登录账号已被拒绝使用，等待先退出账号..."
                    time.sleep(1)
                    continue
                if confirm_file:
                    try:
                        confirm_file.parent.mkdir(parents=True, exist_ok=True)
                        if confirm_file.exists():
                            confirm_file.unlink()
                    except Exception:
                        pass
                    print(json.dumps({
                        "authConfirm": True,
                        "href": data.get("href"),
                        "tokenSource": data.get("tokenSource"),
                    }, ensure_ascii=False), flush=True)
                    wait_until = time.time() + max(30, min(300, login_timeout))
                    decision = None
                    while time.time() < wait_until:
                        try:
                            if confirm_file.exists():
                                decision = json.loads(confirm_file.read_text(encoding="utf-8"))
                                try:
                                    confirm_file.unlink()
                                except Exception:
                                    pass
                                break
                        except Exception:
                            pass
                        time.sleep(0.3)
                    if not decision or not bool(decision.get("use")):
                        rejected_token = current_token
                        waiting_logout_after_reject = True
                        print("[auth] 当前 Suno 账号未确认使用，请在浏览器里退出旧账号并登录新账号。", flush=True)
                        time.sleep(2)
                        continue
                token_file.write_text(json.dumps(data, ensure_ascii=False, indent=2))
                print(f"[auth] Suno login OK tokenSource={data.get('tokenSource')} href={data.get('href')}",
                      flush=True)
                if close_after_success:
                    close_cdp_browser()
                return data
            if data.get("token") and not data.get("projectKeys"):
                last_error = "已登录，但还没有拿到 Studio project id，正在等待 Studio 项目加载..."
                try:
                    if require_project_keys:
                        browser_navigate("https://suno.com/studio")
                except Exception:
                    pass
            else:
                last_error = f"没有拿到 Suno 登录 token：{data.get('tokenMeta')}"
        except Exception as e:
            last_error = str(e)
        if time.time() - last_prompt > 8:
            print("[auth] 请在 Fengling 专用浏览器窗口里登录 Suno；普通 Chrome 已登录不会自动共享到这个专用资料夹。登录完成后这里会自动继续。", flush=True)
            if last_error:
                print(f"[auth] waiting: {last_error}", flush=True)
            last_prompt = time.time()
        time.sleep(3)
    raise RuntimeError(f"等待 Suno 登录超时：{last_error}")


def move_chrome_away():
    try:
        browser_navigate("about:blank")
        time.sleep(1.5)
    except Exception:
        pass


def reopen_song(clip_id: str):
    try:
        browser_navigate(f"https://suno.com/song/{clip_id}")
    except Exception:
        pass


def reopen_studio():
    try:
        browser_navigate("https://suno.com/studio")
    except Exception:
        pass


def open_url_default_browser(url: str) -> None:
    try:
        if IS_WINDOWS:
            os.startfile(url)  # type: ignore[attr-defined]
        else:
            subprocess.run(["open" if IS_DARWIN else "xdg-open", url],
                           check=False, text=True, capture_output=True, timeout=20)
    except Exception as e:
        print(f"[open] default browser skipped: {e}", flush=True)


# ── token helpers ─────────────────────────────────────────────────────────────

def now_ms() -> int:
    return int(time.time() * 1000)


def write_json_atomic(path: pathlib.Path, data: Any,
                      best_effort: bool = False) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    last_error: Optional[BaseException] = None
    for attempt in range(15):
        temp_path = path.with_name(
            f"{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temp_path.write_text(payload, encoding="utf-8")
            os.replace(temp_path, path)
            return True
        except (PermissionError, OSError) as e:
            last_error = e
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass
            if attempt < 14:
                time.sleep(min(0.5, 0.04 * (attempt + 1)))
    if best_effort:
        print(f"[progress-warning] progress file is temporarily locked; "
              f"continue without aborting: {path.name}: {last_error}", flush=True)
        return False
    if last_error is not None:
        raise last_error
    return False


def browser_token() -> str:
    inner = json.dumps({"timestamp": now_ms()}, separators=(",", ":")).encode()
    return json.dumps({"token": base64.b64encode(inner).decode()}, separators=(",", ":"))


# ── Suno API client ───────────────────────────────────────────────────────────

def clean_device_id(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9-]", "", s or "")


class SunoClient:
    def __init__(self, token: str, device_id: str = "", token_refresh=None):
        self.token         = token
        self.device_id     = clean_device_id(device_id)
        self.token_refresh = token_refresh
        self._local        = threading.local()
        self._auth_lock    = threading.Lock()

    def session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            self._local.session = session
        return session

    def headers(self, json_body: bool = False) -> Dict[str, str]:
        h = {
            "Authorization": "Bearer " + self.token,
            "Browser-Token":  browser_token(),
            "Origin":         "https://suno.com",
            "Referer":        "https://suno.com/studio",
            "User-Agent":     ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/136.0.0.0 Safari/537.36"),
            "Accept":         "application/json, text/plain, */*",
        }
        if self.device_id:
            h["Device-Id"] = self.device_id
        if json_body:
            h["Content-Type"] = "application/json"
        return h

    def api(self, method: str, path: str, body: Any = None, timeout: int = 60) -> Any:
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        r = None
        refreshed_auth = False
        for attempt in range(10):
            request_token = self.token
            try:
                r = self.session().request(
                    method, API + path,
                    headers=self.headers(body is not None),
                    data=data, timeout=timeout,
                )
            except requests.RequestException as e:
                wait = min(60.0, 3.0 + attempt * 6.0)
                print(f"[api-retry] {method} {path} -> {type(e).__name__}: {e}; sleep {wait:.1f}s",
                      flush=True)
                time.sleep(wait)
                continue
            if r.status_code == 429:
                retry_after = r.headers.get("Retry-After")
                try:
                    wait = float(retry_after) if retry_after else 0.0
                except Exception:
                    wait = 0.0
                wait = max(wait, min(45.0, 2.0 + attempt * 4.0))
                print(f"[rate-limit] {method} {path} -> 429, sleep {wait:.1f}s", flush=True)
                time.sleep(wait)
                continue
            if r.status_code == 401 and self.token_refresh and not refreshed_auth:
                with self._auth_lock:
                    if self.token == request_token:
                        print(f"[auth-refresh] {method} {path} -> 401, refreshing Suno login token...",
                              flush=True)
                        ctx = self.token_refresh()
                        self.token = ctx["token"]
                        self.device_id = clean_device_id(ctx.get("deviceId") or self.device_id)
                refreshed_auth = True
                time.sleep(1.5)
                continue
            if r.status_code in (500, 502, 503, 504) and attempt < 4:
                wait = min(30.0, 2.0 + attempt * 3.0)
                print(f"[server-retry] {method} {path} -> {r.status_code}, sleep {wait:.1f}s",
                      flush=True)
                time.sleep(wait)
                continue
            break
        assert r is not None
        if not r.ok:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:600]}")
        if not r.text:
            return None
        try:
            return r.json()
        except Exception:
            return r.text


def load_clip_cleanup_rows(path: pathlib.Path) -> List[Dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(data, dict):
        rows = data.get("clips") or data.get("rows") or []
    elif isinstance(data, list):
        rows = data
    else:
        rows = []
    out: List[Dict[str, Any]] = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        clip_id = str(row.get("clipId") or row.get("clip_id") or "").strip()
        file_name = str(row.get("fileName") or row.get("file_name") or "").strip()
        if not clip_id or clip_id in seen:
            continue
        if not re.search(r"_part\d+_", file_name, re.I):
            continue
        seen.add(clip_id)
        out.append({
            "clipId": clip_id,
            "uploadId": row.get("uploadId") or row.get("upload_id"),
            "fileName": file_name,
            "runDir": row.get("runDir") or row.get("run_dir"),
            "title": row.get("title"),
        })
    return out


def delete_one_uploaded_clip(client: SunoClient, clip_id: str) -> tuple[bool, str]:
    attempts = [
        ("POST", "/api/gen/trash", {"trash": True, "clip_ids": [clip_id]}),
    ]
    last_error = ""
    for method, path, body in attempts:
        try:
            client.api(method, path, body, timeout=60)
            return True, f"{method} {path}"
        except Exception as e:
            last_error = str(e)
            if "-> 404:" in last_error:
                return True, "already missing"
            if "-> 403:" in last_error:
                break
    return False, last_error[:260]


def trash_uploaded_clip_batch(client: SunoClient, rows: List[Dict[str, Any]]) -> tuple[int, List[Dict[str, Any]]]:
    clip_ids = [str(row.get("clipId") or "") for row in rows if row.get("clipId")]
    if not clip_ids:
        return 0, []
    try:
        client.api("POST", "/api/gen/trash", {"trash": True, "clip_ids": clip_ids}, timeout=90)
        return len(clip_ids), []
    except Exception as e:
        failed = []
        deleted = 0
        for row in rows:
            clip_id = str(row.get("clipId") or "")
            file_name = str(row.get("fileName") or clip_id)
            ok, detail = delete_one_uploaded_clip(client, clip_id)
            if ok:
                deleted += 1
            else:
                failed.append({"clipId": clip_id, "fileName": file_name, "error": detail or str(e)[:260]})
        return deleted, failed


def looks_like_uploaded_slice_clip(clip: Dict[str, Any]) -> bool:
    if not isinstance(clip, dict):
        return False
    if clip.get("is_trashed"):
        return False
    metadata = clip.get("metadata") if isinstance(clip.get("metadata"), dict) else {}
    if metadata.get("type") != "upload":
        return False
    title = str(clip.get("title") or "")
    return bool(re.search(r"_part\d+_", title, re.I))


def scan_suno_library_slice_clips(client: SunoClient, max_pages: int = 50) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    seen = set()
    limit = 20
    for page in range(max_pages):
        data = client.api("GET", f"/api/feed/v2?is_liked=false&limit={limit}&page={page}", timeout=60)
        clips = data.get("clips") if isinstance(data, dict) else []
        if not isinstance(clips, list):
            clips = []
        for clip in clips:
            if not looks_like_uploaded_slice_clip(clip):
                continue
            clip_id = str(clip.get("id") or "").strip()
            if not clip_id or clip_id in seen:
                continue
            seen.add(clip_id)
            rows.append({
                "clipId": clip_id,
                "fileName": str(clip.get("title") or clip_id),
                "title": str(clip.get("title") or ""),
                "source": "suno-library",
            })
        if not (isinstance(data, dict) and data.get("has_more")):
            break
    return rows


def scan_suno_library_clip_ids(client: SunoClient, max_pages: int = 50) -> set[str]:
    ids: set[str] = set()
    limit = 20
    for page in range(max_pages):
        data = client.api("GET", f"/api/feed/v2?is_liked=false&limit={limit}&page={page}", timeout=60)
        clips = data.get("clips") if isinstance(data, dict) else []
        if not isinstance(clips, list):
            clips = []
        for clip in clips:
            clip_id = str(clip.get("id") or "").strip() if isinstance(clip, dict) else ""
            if clip_id:
                ids.add(clip_id)
        if not (isinstance(data, dict) and data.get("has_more")):
            break
    return ids


def cleanup_song_count(rows: List[Dict[str, Any]]) -> int:
    songs = set()
    for row in rows:
        name = str(row.get("fileName") or row.get("title") or "").strip()
        m = re.search(r"^(.*?)_part\d+_", name, re.I)
        if m and m.group(1).strip():
            songs.add(m.group(1).strip().lower())
        elif name:
            songs.add(name.lower())
    return len(songs)


def merge_cleanup_rows(*row_groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    seen = set()
    for rows in row_groups:
        for row in rows:
            clip_id = str(row.get("clipId") or "").strip()
            if not clip_id or clip_id in seen:
                continue
            seen.add(clip_id)
            merged.append(row)
    return merged


def make_suno_client_from_saved_login(purpose: str) -> SunoClient:
    ctx = load_saved_login(DEFAULT_TOKEN_FILE, max_age_sec=86400 * 7)
    if not ctx:
        raise RuntimeError(f"没有可用的 Suno 登录状态，请先连接 Suno后再{purpose}。")

    def refresh_suno_token() -> Dict[str, Any]:
        if os.environ.get("SUNO_NO_BROWSER_ON_UPLOAD", "0") == "1":
            raise RuntimeError(f"Suno 登录状态已失效，请先点击[连接 Suno]重新连接后再{purpose}。")
        return export_chrome_suno_token(
            DEFAULT_TOKEN_FILE, 180,
            require_project_keys=False,
            login_url="https://suno.com/",
        )

    return SunoClient(ctx["token"], ctx.get("deviceId") or "", token_refresh=refresh_suno_token)


def scan_uploaded_clips_for_cleanup() -> None:
    print("[cleanup] scanning current Suno library slice clips", flush=True)
    client = make_suno_client_from_saved_login("扫描切片")
    rows = scan_suno_library_slice_clips(client)
    result = {
        "cleanupScanOk": True,
        "total": len(rows),
        "songCount": cleanup_song_count(rows),
        "clips": rows,
    }
    print(json.dumps(result, ensure_ascii=False), flush=True)


def cleanup_uploaded_clips(request_path: pathlib.Path) -> None:
    print("[cleanup] preparing current Suno library slice cleanup", flush=True)
    client = make_suno_client_from_saved_login("清理切片")
    request_rows = load_clip_cleanup_rows(request_path) if request_path.exists() else []
    if request_rows:
        print(f"[cleanup] loaded {len(request_rows)} confirmed online slice clips", flush=True)
        rows = merge_cleanup_rows(request_rows)
    else:
        print("[cleanup] no confirmed list found; scanning current Suno library only", flush=True)
        library_rows = scan_suno_library_slice_clips(client)
        print(f"[cleanup] scanned {len(library_rows)} current library slice clips", flush=True)
        rows = merge_cleanup_rows(library_rows)
    print(f"[cleanup] total unique slice clips to trash: {len(rows)}", flush=True)
    if not rows:
        print(json.dumps({"cleanupOk": True, "total": 0, "deleted": 0, "failed": 0},
                         ensure_ascii=False), flush=True)
        return

    deleted = 0
    failed: List[Dict[str, Any]] = []
    batch_size = 20
    for start in range(0, len(rows), batch_size):
        batch = rows[start:start + batch_size]
        end = start + len(batch)
        print(f"[cleanup {end}/{len(rows)}] trashing batch {start + 1}-{end}", flush=True)
        batch_deleted, batch_failed = trash_uploaded_clip_batch(client, batch)
        deleted += batch_deleted
        failed.extend(batch_failed)
        print(f"[cleanup {end}/{len(rows)}] trashed {batch_deleted}/{len(batch)}", flush=True)
    result = {"cleanupOk": True, "total": len(rows), "deleted": deleted,
              "failed": len(failed), "failedItems": failed[:30]}
    print(json.dumps(result, ensure_ascii=False), flush=True)


# ── audio splitting ───────────────────────────────────────────────────────────

class UploadProcessingError(RuntimeError):
    def __init__(self, seg: "Segment", status_payload: Dict[str, Any]):
        self.seg            = seg
        self.status_payload = status_payload
        super().__init__(
            f"{seg.fileName} processing failed: "
            f"{json.dumps(status_payload, ensure_ascii=False)[:800]}"
        )


@dataclass
class Segment:
    index:           Any
    path:            pathlib.Path
    fileName:        str
    srcStartSec:     float
    srcEndSec:       float
    nominalStartSec: float
    nominalEndSec:   float
    timelineStartSec: float
    timelineEndSec:  float
    duration:        float
    fadeInSec:       float
    fadeOutSec:      float


def _auto_download_ffmpeg() -> Optional[str]:
    """Download a static ffmpeg build into bin/ automatically on Windows."""
    if not IS_WINDOWS:
        return None
    bin_dir = ROOT / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    out = bin_dir / "ffmpeg.exe"
    print("[ffmpeg] ffmpeg.exe not found - auto-downloading static build...", flush=True)
    # Use GitHub releases of ffmpeg-master-latest-win64-gpl (small essentials build)
    urls = [
        "https://github.com/BtbN/ffmpeg-builds/releases/download/latest/ffmpeg-master-latest-win64-gpl-shared.zip",
        "https://github.com/BtbN/ffmpeg-builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
    ]
    import urllib.request, zipfile, io, tempfile
    for url in urls:
        try:
            print(f"[ffmpeg] downloading from {url.split('/')[4]}/...", flush=True)
            with urllib.request.urlopen(url, timeout=120) as resp:
                data = resp.read()
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                # Find ffmpeg.exe inside the zip
                candidates = [n for n in zf.namelist()
                              if n.endswith("/bin/ffmpeg.exe") or n == "ffmpeg.exe"]
                if not candidates:
                    candidates = [n for n in zf.namelist() if n.lower().endswith("ffmpeg.exe")]
                if not candidates:
                    continue
                # Pick shortest path (least nested)
                name = sorted(candidates, key=len)[0]
                out.write_bytes(zf.read(name))
            print(f"[ffmpeg] saved to {out}", flush=True)
            return str(out)
        except Exception as e:
            print(f"[ffmpeg] download attempt failed: {e}", flush=True)
            continue
    return None


def find_ffmpeg() -> str:
    names = ["ffmpeg.exe", "ffmpeg"] if IS_WINDOWS else ["ffmpeg", "ffmpeg.exe"]
    candidates: List[pathlib.Path] = []
    for name in names:
        candidates += [
            ROOT / "bin" / name,
            pathlib.Path(sys.executable).resolve().parent / "bin" / name,
        ]
        if getattr(sys, "frozen", False):
            try:
                candidates.append(pathlib.Path(getattr(sys, "_MEIPASS")) / "bin" / name)
            except Exception:
                pass
    for p in candidates:
        if p.exists():
            return str(p)
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    try:
        import imageio_ffmpeg  # type: ignore
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    # Last resort: auto-download on Windows
    auto = _auto_download_ffmpeg()
    if auto and pathlib.Path(auto).exists():
        return auto
    raise RuntimeError(
        "ffmpeg not found. Please download ffmpeg.exe from https://ffmpeg.org/download.html and place it in the bin folder."
    )


def decode_to_wav(src: pathlib.Path, wav_path: pathlib.Path):
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"[decode] {src} -> {wav_path}", flush=True)
    if IS_DARWIN and pathlib.Path("/usr/bin/afconvert").exists():
        subprocess.run(
            ["/usr/bin/afconvert", "-f", "WAVE", "-d", "LEI16@44100", str(src), str(wav_path)],
            check=True
        )
        return
    ffmpeg = find_ffmpeg()
    subprocess.run([
        ffmpeg, "-y", "-i", str(src),
        "-acodec", "pcm_s16le", "-ar", "44100",
        str(wav_path),
    ], check=True)


def read_wav_np(path: pathlib.Path):
    with wave.open(str(path), "rb") as w:
        ch  = w.getnchannels()
        rate = w.getframerate()
        sw  = w.getsampwidth()
        n   = w.getnframes()
        if sw != 2:
            raise RuntimeError(f"只支持 16-bit PCM WAV, got sampwidth={sw}")
        raw = w.readframes(n)
    arr = np.frombuffer(raw, dtype="<i2").reshape(-1, ch)
    return arr, rate, ch


def write_wav_np(path: pathlib.Path, arr: np.ndarray, rate: int, ch: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    arr = np.ascontiguousarray(np.clip(arr, -32768, 32767).astype("<i2"))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(arr.tobytes())


def local_rms_envelope(arr: np.ndarray, rate: int,
                       step_ms: int = 10, rms_ms: int = 50
                       ) -> tuple[np.ndarray, float]:
    if arr.ndim == 2:
        mono = arr.astype(np.float32).mean(axis=1)
    else:
        mono = arr.astype(np.float32)
    if len(mono) == 0:
        raise ValueError("empty audio array")
    step = max(1, int(step_ms / 1000.0 * rate))
    win = max(1, int(rms_ms / 1000.0 * rate))
    half = win // 2
    sq = mono.astype(np.float64) ** 2
    cumsq = np.concatenate(([0.0], np.cumsum(sq)))
    centers = np.arange(0, len(mono), step, dtype=np.int64)
    a = np.clip(centers - half, 0, len(mono))
    b = np.clip(centers + half, 0, len(mono))
    lengths = np.maximum(1, b - a).astype(np.float64)
    rms = np.sqrt((cumsq[b] - cumsq[a]) / lengths).astype(np.float32)
    return rms, step / float(rate)


def local_split_plan(arr: np.ndarray, rate: int,
                     target_sec: float, max_sec: float,
                     search_sec: float, overlap_sec: float
                     ) -> tuple[List[float], Dict[str, Any]]:
    duration = len(arr) / float(rate)
    rms, step_sec = local_rms_envelope(arr, rate)
    # Middle segments have overlap on both sides, so reserve that space here.
    nominal_max = max(0.5, float(max_sec) - 2.0 * max(0.0, float(overlap_sec)))
    target = min(float(target_sec), nominal_max)
    search = max(0.0, float(search_sec))
    min_piece = max(0.35, min(1.0, target * 0.35))
    cuts = [0.0]
    cur = 0.0
    while duration - cur > nominal_max:
        ideal = cur + target
        lo = max(cur + min_piece, ideal - search)
        hi = min(cur + nominal_max, ideal + search, duration)
        a = max(0, int(lo / step_sec))
        b = min(len(rms) - 1, int(hi / step_sec))
        if b <= a:
            cut = min(cur + nominal_max, duration)
        else:
            window = rms[a:b + 1]
            # Prefer a late, relatively quiet point. Pure minimum-energy
            # selection creates too many short chunks and slows the upload.
            quiet_limit = float(np.percentile(window, 80))
            candidates = np.flatnonzero(window <= quiet_limit)
            best = int(candidates[-1]) if len(candidates) else int(np.argmin(window))
            cut = int(a + best) * step_sec
        cut = max(cur + min_piece, min(cut, cur + nominal_max, duration))
        if 0.0 < duration - cut < min_piece:
            cut = max(cur + min_piece, duration - min_piece)
        if cut <= cur:
            break
        cuts.append(round(cut, 3))
        cur = cut
    if cuts[-1] < duration:
        cuts.append(round(duration, 3))
    params = {
        "target_sec": target,
        "max_sec": max_sec,
        "search_sec": search_sec,
        "overlap_sec": overlap_sec,
    }
    return cuts, {
        "algo_version": "local-low-energy-1.0",
        "params_used": params,
    }


def split_audio(src: pathlib.Path, out_dir: pathlib.Path,
                target_sec: float, max_sec: float,
                search_sec: float, overlap_sec: float
                ) -> tuple[List[Segment], Dict[str, Any]]:
    wav_full = out_dir / "decoded_original.wav"
    decode_to_wav(src, wav_full)
    arr, rate, ch = read_wav_np(wav_full)
    total_sec = len(arr) / rate
    print(f"[split] decoded duration={total_sec:.3f}s rate={rate} ch={ch}", flush=True)

    cuts, algo_resp = local_split_plan(
        arr, rate,
        target_sec=target_sec, max_sec=max_sec,
        search_sec=search_sec, overlap_sec=overlap_sec,
    )
    params_used = algo_resp.get("params_used") or {}
    print(f"[split] local cuts={len(cuts)-1} segments, "
          f"params={params_used}, algo={algo_resp.get('algo_version')}",
          flush=True)

    segs: List[Segment] = []
    stem = re.sub(r"[^a-zA-Z0-9_\-.]+", "_", src.stem)[:50]
    for i in range(len(cuts) - 1):
        nominal_start = cuts[i]
        nominal_end   = cuts[i + 1]
        src_start = max(0.0, nominal_start - (overlap_sec if i > 0 else 0.0))
        src_end   = min(total_sec, nominal_end + (overlap_sec if i < len(cuts) - 2 else 0.0))
        a, b      = int(round(src_start * rate)), int(round(src_end * rate))
        seg_arr   = arr[a:b]
        name      = f"{stem}_part{i+1:02d}_{nominal_start:.2f}s-{nominal_end:.2f}s.wav"
        path      = out_dir / "segments" / name
        write_wav_np(path, seg_arr, rate, ch)
        segs.append(Segment(
            index=i + 1, path=path, fileName=name,
            srcStartSec=src_start, srcEndSec=src_end,
            nominalStartSec=nominal_start, nominalEndSec=nominal_end,
            timelineStartSec=src_start, timelineEndSec=src_end,
            duration=src_end - src_start,
            fadeInSec=(2 * overlap_sec if i > 0 else 0.0),
            fadeOutSec=(2 * overlap_sec if i < len(cuts) - 2 else 0.0),
        ))
        print(f"[split] {name} src={src_start:.3f}-{src_end:.3f}s "
              f"duration={src_end-src_start:.3f}s "
              f"fadeIn={segs[-1].fadeInSec:.3f} fadeOut={segs[-1].fadeOutSec:.3f}",
              flush=True)

    manifest = {
        "source": str(src), "decodedWav": str(wav_full),
        "sourceSize": src.stat().st_size,
        "sourceMtimeNs": src.stat().st_mtime_ns,
        "duration": total_sec, "rate": rate, "channels": ch,
        "targetSec": target_sec, "maxSec": max_sec,
        "searchSec": search_sec, "overlapSec": overlap_sec,
        "cuts": cuts,
        "algoVersion": algo_resp.get("algo_version"),
        "paramsUsed":  params_used,
        "segments": [s.__dict__ | {"path": str(s.path)} for s in segs],
    }
    write_json_atomic(out_dir / "split_manifest.json", manifest)
    return segs, manifest


def split_failed_segment(seg: Segment, overlap_sec: float, depth: int) -> List[Segment]:
    arr, rate, ch = read_wav_np(seg.path)
    dur = len(arr) / rate
    if dur <= 0.35:
        raise RuntimeError(f"cannot split very short failed segment: {seg.fileName} {dur:.3f}s")

    rms, step_sec = local_rms_envelope(arr, rate)
    lo = max(0, int((dur * 0.35) / step_sec))
    hi = min(len(rms) - 1, int((dur * 0.65) / step_sec))
    if hi > lo:
        window = rms[lo:hi + 1]
        quiet_limit = float(np.percentile(window, 50))
        candidates = np.flatnonzero(window <= quiet_limit)
        midpoint = int((dur / 2.0) / step_sec) - lo
        best = min((int(x) for x in candidates), key=lambda x: abs(x - midpoint))
        cut = (lo + best) * step_sec
    else:
        cut = dur / 2.0
    ov = min(float(overlap_sec), max(0.0, dur / 8.0))
    rels_in = [
        {
            "suffix": "a",
            "rel_start": 0.0,
            "rel_end": round(min(dur, cut + ov), 3),
            "fade_in_inherit_from_parent": True,
            "fade_out_sec": round(ov * 2, 3),
        },
        {
            "suffix": "b",
            "rel_start": round(max(0.0, cut - ov), 3),
            "rel_end": round(dur, 3),
            "fade_in_sec": round(ov * 2, 3),
            "fade_out_inherit_from_parent": True,
        },
    ]

    # 把服务端给的 rel_start/rel_end + 继承父段 fade 的旗标，本地落成具体 fade 值
    def _resolve_fade(side: Dict[str, Any], side_name: str) -> tuple[float, float]:
        if side_name == "a":
            fade_in  = (seg.fadeInSec if side.get("fade_in_inherit_from_parent")
                        else float(side.get("fade_in_sec") or 0.0))
            fade_out = float(side.get("fade_out_sec") or 0.0)
        else:
            fade_in  = float(side.get("fade_in_sec") or 0.0)
            fade_out = (seg.fadeOutSec if side.get("fade_out_inherit_from_parent")
                        else float(side.get("fade_out_sec") or 0.0))
        return fade_in, fade_out

    children: List[Segment] = []
    for side in rels_in:
        suffix     = side.get("suffix") or ""
        rel_start  = float(side["rel_start"])
        rel_end    = float(side["rel_end"])
        fade_in, fade_out = _resolve_fade(side, suffix)
        a, b       = int(round(rel_start * rate)), int(round(rel_end * rate))
        child_arr  = arr[a:b]
        base       = pathlib.Path(seg.fileName).stem
        child_name = f"{base}_retry{depth}{suffix}_{rel_start:.2f}-{rel_end:.2f}s.wav"
        child_path = seg.path.parent / child_name
        write_wav_np(child_path, child_arr, rate, ch)
        children.append(Segment(
            index=f"{seg.index}{suffix}", path=child_path, fileName=child_name,
            srcStartSec=seg.srcStartSec + rel_start, srcEndSec=seg.srcStartSec + rel_end,
            nominalStartSec=seg.nominalStartSec + rel_start,
            nominalEndSec=seg.nominalStartSec + rel_end,
            timelineStartSec=seg.timelineStartSec + rel_start,
            timelineEndSec=seg.timelineStartSec + rel_end,
            duration=rel_end - rel_start,
            fadeInSec=fade_in, fadeOutSec=fade_out,
        ))
    print(
        f"[retry-split] {seg.fileName} {dur:.2f}s -> "
        f"{children[0].fileName} {children[0].duration:.2f}s + "
        f"{children[1].fileName} {children[1].duration:.2f}s "
        f"(cut={cut:.3f}s ov={ov:.3f}s, local)",
        flush=True,
    )
    return children


# ── S3 upload ─────────────────────────────────────────────────────────────────


def s3_post_upload(url: str, fields: Dict[str, Any],
                   seg: Segment, mime: str) -> requests.Response:
    """Upload to S3, bypassing broken Windows system proxies."""
    sess = requests.Session()
    sess.trust_env = False
    with seg.path.open("rb") as fp:
        return sess.post(
            url,
            data=fields or {},
            files={"file": (seg.fileName, fp, mime)},
            headers={"Connection": "close"},
            timeout=(30, 300),
        )


def prepare_upload(client: SunoClient, seg: Segment, total: int) -> Dict[str, Any]:
    print(f"[upload {seg.index}/{total}] init {seg.fileName} {seg.duration:.2f}s", flush=True)
    mime  = mimetypes.guess_type(seg.fileName)[0] or "audio/wav"
    init  = None
    r     = None
    last_err = ""
    for attempt in range(8):
        try:
            init = client.api("POST", "/api/uploads/audio/",
                              {"extension": "wav", "upload_type": "studio_file_upload"},
                              timeout=60)
            r    = s3_post_upload(init["url"], init.get("fields") or {}, seg, mime)
            if r.ok or r.status_code == 204:
                break
            last_err = f"{r.status_code} {r.text[:200]}"
            print(f"[upload {seg.index}/{total}] S3 retry {attempt+1}: {last_err[:120]}", flush=True)
        except requests.RequestException as e:
            last_err = f"{type(e).__name__}: {e}"
            print(f"[upload {seg.index}/{total}] S3 retry {attempt+1}: {last_err}", flush=True)
        time.sleep(min(45.0, 4.0 + attempt * 6.0))

    if init is None or r is None:
        raise RuntimeError(f"S3 upload failed {seg.fileName}: {last_err or 'no response'}")
    if not (r.ok or r.status_code == 204):
        raise RuntimeError(f"S3 upload failed {seg.fileName}: "
                           f"{last_err or (str(r.status_code) + ' ' + r.text[:300])}")

    client.api("POST", f"/api/uploads/audio/{init['id']}/upload-finish/",
               {"upload_type": "studio_file_upload", "upload_filename": seg.fileName},
               timeout=90)
    print(f"[upload {seg.index}/{total}] queued -> uploadId={init['id']}", flush=True)
    return init


def complete_upload(client: SunoClient, seg: Segment, total: int,
                    init: Dict[str, Any], poll_ms: int = 1800,
                    max_poll: int = 150,
                    initialize_clip: bool = True) -> Dict[str, Any]:
    st = None
    for i in range(max_poll):
        try:
            st = client.api("GET", f"/api/uploads/audio/{init['id']}/")
        except RuntimeError as e:
            msg = str(e)
            if "-> 404:" in msg or "-> 429:" in msg:
                if i % 5 == 0:
                    print(f"[upload {seg.index}/{total}] waiting {seg.fileName}: temporary status error {msg[:120]}",
                          flush=True)
                time.sleep(poll_ms / 1000)
                continue
            raise
        status = st.get("status") or ""
        if status == "complete" and st.get("s3_id"):
            break
        if re.search(r"failed|rejected|blocked|error", status, re.I):
            raise UploadProcessingError(seg, st)
        if i % 5 == 0:
            print(f"[upload {seg.index}/{total}] waiting {seg.fileName}: {status}", flush=True)
        time.sleep(poll_ms / 1000)

    if not st or st.get("status") != "complete" or not st.get("s3_id"):
        raise RuntimeError(f"Timeout waiting for {seg.fileName}")

    clip_id = None
    if initialize_clip:
        ic      = client.api("POST", f"/api/uploads/audio/{init['id']}/initialize-clip/", {})
        clip_id = ic.get("clip_id") or st.get("s3_id")
        try:
            client.api("POST", f"/api/gen/{clip_id}/set_metadata/", {
                "title": pathlib.Path(seg.fileName).stem,
                "image_url": st.get("image_url"),
                "is_audio_upload_tos_accepted": True,
            })
        except Exception as e:
            print(f"[upload {seg.index}/{total}] metadata ignored: {e}", flush=True)

    print(f"[upload {seg.index}/{total}] complete -> uploadId={init['id']}"
          + (f" clipId={clip_id}" if clip_id else ""), flush=True)
    return {
        "fileName":        seg.fileName,
        "path":            str(seg.path),
        "duration":        seg.duration,
        "clipId":          clip_id,
        "uploadId":        init["id"],
        "timelineStartSec": seg.timelineStartSec,
        "timelineEndSec":  seg.timelineEndSec,
        "fadeInSec":       seg.fadeInSec,
        "fadeOutSec":      seg.fadeOutSec,
        "nominalStartSec": seg.nominalStartSec,
        "nominalEndSec":   seg.nominalEndSec,
        "imageUrl":        st.get("image_url"),
        "tags":            st.get("display_tags") or st.get("tags") or "",
    }


def upload_one(client: SunoClient, seg: Segment, total: int,
               poll_ms: int = 1800, max_poll: int = 150,
               initialize_clip: bool = True) -> Dict[str, Any]:
    init = prepare_upload(client, seg, total)
    return complete_upload(
        client, seg, total, init,
        poll_ms=poll_ms,
        max_poll=max_poll,
        initialize_clip=initialize_clip,
    )


def row_interval(row: Dict[str, Any]) -> tuple[float, float]:
    return (float(row["timelineStartSec"]), float(row["timelineEndSec"]))


def segment_interval(seg: Segment) -> tuple[float, float]:
    return (float(seg.timelineStartSec), float(seg.timelineEndSec))


def intervals_overlap(a: tuple[float, float], b: tuple[float, float], eps: float = 0.02) -> bool:
    return a[0] < b[1] - eps and b[0] < a[1] - eps


def rows_cover_segment(rows: List[Dict[str, Any]], seg: Segment, eps: float = 0.24) -> bool:
    start, end = segment_interval(seg)
    parts = sorted(
        (max(start, a), min(end, b))
        for a, b in (row_interval(r) for r in rows)
        if intervals_overlap((a, b), (start, end), eps=0.02)
    )
    if not parts:
        return False
    cursor = start
    for a, b in parts:
        if a > cursor + eps:
            return False
        cursor = max(cursor, b)
        if cursor >= end - eps:
            return True
    return cursor >= end - eps


def load_progress_rows(progress_path: Optional[pathlib.Path]) -> List[Dict[str, Any]]:
    if not progress_path or not progress_path.exists():
        return []
    try:
        data = json.loads(progress_path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict) and r.get("uploadId")]
    except Exception as e:
        print(f"[resume] progress ignored: {e}", flush=True)
    return []


def sort_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(rows, key=lambda r: (float(r["timelineStartSec"]), float(r["timelineEndSec"])))


def can_retry_split_error(exc: UploadProcessingError, seg: Segment,
                          min_sec: float, max_depth: int, depth: int) -> bool:
    return bool(
        seg.duration > max(0.35, float(min_sec) * 2.0)
        and int(depth) < int(max_depth)
    )


def upload_all(client: SunoClient, segs: List[Segment],
               concurrency: int, retry_min_sec: float,
               retry_overlap_sec: float, retry_max_depth: int,
               initialize_clip: bool,
               progress_path: Optional[pathlib.Path] = None) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = load_progress_rows(progress_path)

    def save_progress():
        if progress_path:
            write_json_atomic(progress_path, sort_rows(out), best_effort=True)

    total = len(segs)
    submit_segs: List[Segment] = []
    for seg in segs:
        if rows_cover_segment(out, seg):
            print(f"[resume] skip uploaded segment {seg.index}/{total} {seg.fileName}", flush=True)
            continue
        seg_span = segment_interval(seg)
        kept = [r for r in out if not intervals_overlap(row_interval(r), seg_span)]
        if len(kept) != len(out):
            print(f"[resume] partial progress discarded for segment {seg.index}/{total} {seg.fileName}",
                  flush=True)
            out = kept
        submit_segs.append(seg)
    save_progress()

    if not submit_segs:
        print("[resume] all segments already uploaded; continue to assembly/render.", flush=True)
        return sort_rows(out)

    max_concurrency = max(1, concurrency)
    adaptive = max_concurrency >= 3
    upload_limit = min(2, max_concurrency) if adaptive else max_concurrency
    submit_streak = 0
    upload_queue: List[tuple[Segment, int]] = [(seg, 0) for seg in submit_segs]
    process_queue: List[tuple[Segment, int, Dict[str, Any]]] = []

    if adaptive:
        print(f"[pipeline] submit and processing stages separated; "
              f"start with 2 submit lanes and raise to {max_concurrency}.",
              flush=True)

    with cf.ThreadPoolExecutor(max_workers=max_concurrency) as upload_ex, \
            cf.ThreadPoolExecutor(max_workers=max_concurrency) as process_ex:
        upload_pending: Dict[cf.Future, tuple[Segment, int]] = {}
        process_pending: Dict[cf.Future, tuple[Segment, int]] = {}

        while upload_pending or process_pending or upload_queue or process_queue:
            while upload_queue and len(upload_pending) < upload_limit:
                seg, depth = upload_queue.pop(0)
                future = upload_ex.submit(prepare_upload, client, seg, total)
                upload_pending[future] = (seg, depth)
            while process_queue and len(process_pending) < max_concurrency:
                seg, depth, init = process_queue.pop(0)
                future = process_ex.submit(
                    complete_upload, client, seg, total, init,
                    1200, 150, initialize_clip,
                )
                process_pending[future] = (seg, depth)

            all_pending = list(upload_pending.keys()) + list(process_pending.keys())
            if not all_pending:
                continue
            done, _ = cf.wait(all_pending, return_when=cf.FIRST_COMPLETED)
            for fut in done:
                if fut in upload_pending:
                    seg, depth = upload_pending.pop(fut)
                    try:
                        init = fut.result()
                        process_queue.append((seg, depth, init))
                        submit_streak += 1
                        if adaptive and upload_limit < max_concurrency and submit_streak >= 2:
                            if max_concurrency >= 6 and upload_limit <= 2:
                                next_limit = min(4, max_concurrency)
                            elif max_concurrency >= 6 and upload_limit < 4:
                                next_limit = min(4, max_concurrency)
                            elif max_concurrency >= 6 and upload_limit < 6:
                                next_limit = min(6, max_concurrency)
                            else:
                                next_limit = min(upload_limit + 1, max_concurrency)
                            upload_limit = next_limit
                            submit_streak = 0
                            print(f"[pipeline] chunks accepted; raise submit lanes to "
                                  f"{upload_limit}.", flush=True)
                    except RuntimeError as e:
                        fallback_limit = 3 if max_concurrency >= 4 else 2
                        if adaptive and upload_limit != fallback_limit:
                            print(f"[pipeline] submit error; reduce submit lanes to "
                                  f"{fallback_limit}.", flush=True)
                        upload_limit = fallback_limit
                        submit_streak = 0
                        msg = str(e)
                        if (("S3 upload failed" in msg or "RequestTimeout" in msg
                             or "Connection timed out" in msg) and depth < 3):
                            print(f"[upload {seg.index}/{total}] network retry whole chunk "
                                  f"depth={depth+1}", flush=True)
                            time.sleep(8 + depth * 8)
                            upload_queue.append((seg, depth + 1))
                        else:
                            raise
                    continue

                seg, depth = process_pending.pop(fut)
                try:
                    out.append(fut.result())
                    save_progress()
                except UploadProcessingError as e:
                    if adaptive and upload_limit > 3:
                        print("[pipeline] processing error; reduce submit lanes to 3.",
                              flush=True)
                        upload_limit = 3
                        submit_streak = 0
                    if can_retry_split_error(e, seg, retry_min_sec, retry_max_depth, depth):
                        children = split_failed_segment(seg, retry_overlap_sec, depth + 1)
                        for child in children:
                            upload_queue.append((child, depth + 1))
                    else:
                        raise
                except RuntimeError as e:
                    if adaptive and upload_limit > 3:
                        fallback_limit = 3 if max_concurrency >= 4 else 2
                        print(f"[pipeline] processing network error; reduce submit lanes to "
                              f"{fallback_limit}.", flush=True)
                        upload_limit = fallback_limit
                        submit_streak = 0
                    msg = str(e)
                    if (("S3 upload failed" in msg or "RequestTimeout" in msg
                         or "Connection timed out" in msg) and depth < 3):
                        print(f"[upload {seg.index}/{total}] network retry whole chunk "
                              f"depth={depth+1}", flush=True)
                        time.sleep(8 + depth * 8)
                        upload_queue.append((seg, depth + 1))
                    else:
                        raise

    return sort_rows(out)


# ── Studio assembly ───────────────────────────────────────────────────────────

def downbeats(bps: float, start: float, end: float):
    a = []
    b = int(np.ceil(start))
    while b < int(np.floor(end)):
        a.append([b / bps - start / bps, ((b % 4 + 4) % 4) + 1])
        b += 1
    return a


def make_clip(row: Dict[str, Any], bps: float, color: str,
              clip_reference: str = "clip-id") -> tuple[Dict[str, Any], str]:
    start          = float(row["timelineStartSec"]) * bps
    end            = float(row["timelineEndSec"]) * bps
    fade_in        = float(row.get("fadeInSec") or 0) * bps
    fade_out       = float(row.get("fadeOutSec") or 0) * bps
    marker_hash    = "warp_" + uuid.uuid4().hex
    duration_beats = max(0.01, float(row["duration"]) * bps)
    use_upload_id  = clip_reference == "upload-id"
    clip = {
        "type": "audio", "streaming": False, "name": row["fileName"], "color": color,
        "amplitude": 1, "transposition": 0, "readStartBeats": 0,
        "fadeInBeats": fade_in, "fadeOutBeats": fade_out,
        "fadeInCurve": 1, "fadeOutCurve": 1,
        "startBeats": start, "endBeats": end,
        "loop": {"startBeats": 0, "endBeats": duration_beats, "enabled": False},
        "warp": {"enabled": False, "awaitingAnalysis": True, "markersHash": marker_hash},
        "id":       str(uuid.uuid4()),
        "clipId":   None if use_upload_id else row["clipId"],
        "uploadId": row["uploadId"] if use_upload_id else None,
        "mute": False, "reversed": False, "awaitingContentAlignment": False,
    }
    return clip, marker_hash


def fallback_project_state(out_dir: pathlib.Path) -> Dict[str, Any]:
    candidates: List[pathlib.Path] = []
    explicit = ROOT / "evidence" / "studio_blank_state_template.json"
    if explicit.exists():
        candidates.append(explicit)
    candidates += sorted(
        (ROOT / "derived" / "auto_recut_run").glob("*/project_before_recut.json"),
        key=lambda p: p.stat().st_mtime, reverse=True)
    candidates += sorted(
        (ROOT / "derived" / "auto_recut_run").glob("*/save_project_recut_payload.json"),
        key=lambda p: p.stat().st_mtime, reverse=True)
    last_error = ""
    for p in candidates:
        try:
            j     = json.loads(p.read_text())
            state = j.get("state") if isinstance(j, dict) else None
            if isinstance(state, dict) and state.get("tracks"):
                state = copy.deepcopy(state)
                (out_dir / "project_state_template_source.txt").write_text(str(p))
                print(f"[project] new project has no state; using local template from {p}", flush=True)
                return state
        except Exception as e:
            last_error = f"{p}: {e}"
    raise RuntimeError(f"新建项目没有 state，且找不到本地 Studio state 模板：{last_error}")


def assemble_render(client: SunoClient, project_id: str,
                    rows: List[Dict[str, Any]], title: str,
                    out_dir: pathlib.Path, render: bool = True,
                    clip_reference: str = "clip-id",
                    track_count: int = 2) -> Dict[str, Any]:
    proj  = client.api("GET", f"/api/studio/project/{project_id}")
    (out_dir / "project_before_recut.json").write_text(
        json.dumps(proj, ensure_ascii=False, indent=2))
    state = (copy.deepcopy(proj["state"])
             if isinstance(proj, dict) and isinstance(proj.get("state"), dict)
             else fallback_project_state(out_dir))
    bps        = float((state.get("timing") or {}).get("bps") or 2.0)
    base_track = next((t for t in state.get("tracks", [])
                       if t.get("type") == "audio"),
                      state.get("tracks", [None])[0])
    if not base_track:
        raise RuntimeError("项目没有轨道")

    track_count = max(1, int(track_count or 1))
    palette     = ["#02AF4A", "#3B82F6", "#A855F7", "#F59E0B"]
    tracks      = []
    for i in range(track_count):
        t        = copy.deepcopy(base_track)
        t["id"]  = str(uuid.uuid4()) if i else base_track.get("id", str(uuid.uuid4()))
        t["name"] = (f"Auto Mix {i + 1}" if track_count > 1
                     else (base_track.get("name") or "New Track"))
        t["color"]  = palette[i % len(palette)]
        t["clips"]  = []
        t["solo"]   = False
        t["mute"]   = False
        t["arm"]    = False
        if "takeLanes"            in t: t["takeLanes"]            = []
        if "clipCreationIntents"  in t: t["clipCreationIntents"]  = []
        tracks.append(t)

    regs = {}
    for i, row in enumerate(rows):
        t       = tracks[i % track_count]
        c, mh   = make_clip(row, bps, t.get("color") or "#02AF4A", clip_reference)
        t["clips"].append(c)
        regs[mh] = {"0": 0}

    state["tracks"]          = tracks
    state["markersRegistry"] = regs
    state["selection"] = {
        **(state.get("selection") or {}),
        "focusedArea":    "timeline",
        "focusedTrackId": tracks[0].get("id"),
    }

    total_sec  = max(float(r["timelineEndSec"]) for r in rows)
    end_beats  = total_sec * bps
    if isinstance(state.get("loop"), dict):
        state["loop"]["startBeats"] = 0
        state["loop"]["endBeats"]   = max(8, end_beats)
        state["loop"]["enabled"]    = False

    payload = {
        "project_id": project_id,
        "state":      state,
        "title":      proj.get("title") or "Untitled Project",
    }
    (out_dir / "save_project_recut_payload.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"[save] project={project_id} clips={len(rows)} tracks={track_count} "
          f"total={total_sec:.2f}s ref={clip_reference}", flush=True)

    save = client.api("POST", "/api/studio/save-project", payload, timeout=120)
    (out_dir / "save_project_recut_response.json").write_text(
        json.dumps(save, ensure_ascii=False, indent=2))

    render_resp = None
    final_clip  = None
    if render:
        rp = {
            "title":                   title,
            "lyrics":                  "",
            "state":                   state,
            "project_id":              project_id,
            "from_studio_project_id":  project_id,
            "start_beats":             0,
            "end_beats":               end_beats,
            "downbeats":               downbeats(bps, 0, end_beats),
            "web_client_pathname":     "/studio",
            "export_mode":             "rendered_context_window",
        }
        (out_dir / "render_state_recut_payload.json").write_text(
            json.dumps(rp, ensure_ascii=False, indent=2))
        print("[render] calling render-state...", flush=True)
        render_resp = client.api("POST", "/api/studio/render-state", rp, timeout=180)
        (out_dir / "render_state_recut_response.json").write_text(
            json.dumps(render_resp, ensure_ascii=False, indent=2))
        rid = render_resp.get("id") if isinstance(render_resp, dict) else None
        print(f"[render] queued id={rid}", flush=True)
        if rid:
            for i in range(120):
                feed = client.api("POST", "/api/feed/v3", {
                    "filters": {"ids": {"presence": "True", "clipIds": [rid]}},
                    "limit": 1,
                }, timeout=60)
                clips2 = feed.get("clips") or []
                if clips2:
                    final_clip = clips2[0]
                    print(f"[render] poll {i}: {final_clip.get('status')} "
                          f"audio={bool(final_clip.get('audio_url'))}", flush=True)
                    if final_clip.get("status") in ("complete", "error"):
                        break
                time.sleep(5)
            if final_clip:
                (out_dir / "render_clip_recut_final.json").write_text(
                    json.dumps(final_clip, ensure_ascii=False, indent=2))

    result = {
        "projectId":    project_id,
        "versionId":    save.get("version_id"),
        "uploaded":     rows,
        "clipCount":    len(rows),
        "totalSeconds": total_sec,
        "render":       render_resp,
        "finalClip":    final_clip,
        "at":           time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (out_dir / "recut_batch_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2))
    return result


def project_id_from_context(ctx: Dict[str, Any]) -> str:
    for k, v in ctx.get("projectKeys", []):
        try:
            j = json.loads(v)
            if j.get("projectId"):
                return j["projectId"]
        except Exception:
            pass
    raise RuntimeError("找不到 projectId")


def create_studio_project(client: SunoClient, title: str,
                          out_dir: pathlib.Path) -> str:
    project_title = title or "Auto Mix Project"
    path  = "/api/studio/create-project?title=" + urllib.parse.quote(project_title, safe="")
    resp  = client.api("POST", path, None, timeout=120)
    (out_dir / "create_project_response.json").write_text(
        json.dumps(resp, ensure_ascii=False, indent=2))
    pid = resp.get("id") if isinstance(resp, dict) else None
    if not pid:
        raise RuntimeError(f"创建 Studio 项目失败：{resp}")
    print(f"[project] created new Studio project id={pid} title={resp.get('title') or project_title}",
          flush=True)
    return pid


# ── main ──────────────────────────────────────────────────────────────────────

def segment_from_manifest(item: Dict[str, Any]) -> Segment:
    data = dict(item)
    path = pathlib.Path(str(data["path"])).expanduser()
    return Segment(
        index=data["index"],
        path=path,
        fileName=str(data["fileName"]),
        srcStartSec=float(data["srcStartSec"]),
        srcEndSec=float(data["srcEndSec"]),
        nominalStartSec=float(data["nominalStartSec"]),
        nominalEndSec=float(data["nominalEndSec"]),
        timelineStartSec=float(data["timelineStartSec"]),
        timelineEndSec=float(data["timelineEndSec"]),
        duration=float(data["duration"]),
        fadeInSec=float(data.get("fadeInSec") or 0.0),
        fadeOutSec=float(data.get("fadeOutSec") or 0.0),
    )


def load_segments_from_manifest(out_dir: pathlib.Path) -> tuple[List[Segment], Dict[str, Any]]:
    manifest_path = out_dir / "split_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    segs = [segment_from_manifest(s) for s in manifest.get("segments") or []]
    if not segs:
        raise RuntimeError(f"resume manifest has no segments: {manifest_path}")
    return segs, manifest


def project_id_from_run_dir(out_dir: pathlib.Path) -> Optional[str]:
    p = out_dir / "create_project_response.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("id"):
            return str(data["id"])
    except Exception:
        pass
    return None


def find_resume_run(work_base: pathlib.Path, src: pathlib.Path) -> Optional[pathlib.Path]:
    if not work_base.exists():
        return None
    src_resolved = str(src.resolve()).lower()
    src_stat = src.stat()
    max_age_sec = max(300, int(os.environ.get("SUNO_RESUME_MAX_AGE_SEC", "86400")))
    cutoff = time.time() - max_age_sec
    candidates: List[pathlib.Path] = []
    for run_dir in sorted((p for p in work_base.iterdir() if p.is_dir()),
                          key=lambda p: p.stat().st_mtime, reverse=True):
        if run_dir.stat().st_mtime < cutoff:
            continue
        if (run_dir / "SUCCESS_SUMMARY.json").exists():
            continue
        manifest_path = run_dir / "split_manifest.json"
        progress_path = run_dir / "uploaded_rows.progress.json"
        if not manifest_path.exists() or not project_id_from_run_dir(run_dir):
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest_source = str(pathlib.Path(str(manifest.get("source") or "")).resolve()).lower()
            if int(manifest.get("sourceSize") or -1) != int(src_stat.st_size):
                continue
            if int(manifest.get("sourceMtimeNs") or -1) != int(src_stat.st_mtime_ns):
                continue
            segment_items = manifest.get("segments") or []
            if not segment_items:
                continue
            if any(not pathlib.Path(str(item.get("path") or "")).exists()
                   for item in segment_items if isinstance(item, dict)):
                continue
        except Exception:
            continue
        if manifest_source == src_resolved and (progress_path.exists() or segment_items):
            candidates.append(run_dir)
    return candidates[0] if candidates else None


def sanitize_upload_argv(argv: List[str]) -> List[str]:
    cleaned: List[str] = []
    i = 0
    known_options_without_values = {
        "--no-render", "--keep-studio-open", "--login-only",
        "--close-login-browser", "--preheat-browser", "--scan-uploaded-clips",
    }
    known_options_with_values = {
        "--title", "--work-dir", "--target-sec", "--max-sec", "--search-sec",
        "--overlap-sec", "--concurrency", "--retry-split-min-sec",
        "--retry-max-depth", "--clip-reference", "--tracks", "--project-mode",
        "--project-id", "--login-timeout", "--delete-uploaded-clips",
    }
    while i < len(argv):
        arg = argv[i]
        if arg == "--title":
            nxt = argv[i + 1] if i + 1 < len(argv) else None
            if not nxt or (nxt.startswith("--") and (
                    nxt in known_options_without_values or nxt in known_options_with_values)):
                print("[args] missing --title value; using source file name as title", flush=True)
                i += 1
                continue
        cleaned.append(arg)
        i += 1
    return cleaned


def main():
    ap = argparse.ArgumentParser(
        description="Auto recut original audio and assemble/render in Suno Studio")
    ap.add_argument("source",           nargs="?",  help="原始音频路径")
    ap.add_argument("--title",          default=None)
    ap.add_argument("--work-dir",       default=str(ROOT / "derived/auto_recut_run"))
    ap.add_argument("--target-sec",     type=float, default=5.4,   help="每段目标长度（秒）")
    ap.add_argument("--max-sec",        type=float, default=5.5,   help="单段最大上传长度（秒）")
    ap.add_argument("--search-sec",     type=float, default=1.0,   help="切点前后搜索低能量范围")
    ap.add_argument("--overlap-sec",    type=float, default=0.08,  help="切点重叠/交叉淡化半宽")
    ap.add_argument("--concurrency",    type=int,   default=6)
    ap.add_argument("--retry-split-min-sec", type=float, default=0.2)
    ap.add_argument("--retry-max-depth",     type=int,   default=10)
    ap.add_argument("--clip-reference", choices=["clip-id", "upload-id"], default="clip-id")
    ap.add_argument("--tracks",         type=int,   default=2)
    ap.add_argument("--project-mode",   choices=["new", "current"],   default="new")
    ap.add_argument("--project-id",     default=None)
    ap.add_argument("--login-timeout",  type=int,   default=600)
    ap.add_argument("--no-render",      action="store_true")
    ap.add_argument("--keep-studio-open", action="store_true")
    ap.add_argument("--login-only",     action="store_true")
    ap.add_argument("--close-login-browser", action="store_true")
    ap.add_argument("--preheat-browser", action="store_true")
    ap.add_argument("--delete-uploaded-clips", default=None,
                    help="删除指定 JSON 文件里的 Suno 上传切片 clipId")
    ap.add_argument("--scan-uploaded-clips", action="store_true",
                    help="只扫描当前 Suno 账号的上传切片，不删除")
    args = ap.parse_args(sanitize_upload_argv(sys.argv[1:]))

    if args.preheat_browser:
        if launch_cdp_browser("about:blank"):
            print("[auth] login browser preheated", flush=True)
            return
        raise SystemExit(1)

    if args.delete_uploaded_clips:
        cleanup_uploaded_clips(pathlib.Path(args.delete_uploaded_clips).expanduser().resolve())
        return

    if args.scan_uploaded_clips:
        scan_uploaded_clips_for_cleanup()
        return

    if args.login_only:
        print("[auth] opening login browser and waiting for Suno login...", flush=True)
        reuse_login_browser = os.environ.get("SUNO_REUSE_LOGIN_BROWSER", "0") == "1"
        if not reuse_login_browser:
            reset_cdp_browser()
        ctx = export_chrome_suno_token(
            DEFAULT_TOKEN_FILE, args.login_timeout,
            require_project_keys=False,
            login_url="https://suno.com/",
            close_after_success=args.close_login_browser,
        )
        print(json.dumps({
            "loginOk": True,
            "tokenSource": ctx.get("tokenSource"),
            "href": ctx.get("href"),
        }, ensure_ascii=False), flush=True)
        return

    if not args.source:
        raise SystemExit("源文件不存在: 未选择音频")
    src = pathlib.Path(args.source).expanduser().resolve()
    if not src.exists():
        raise SystemExit(f"源文件不存在: {src}")

    work_base = pathlib.Path(args.work_dir).expanduser().resolve()
    resume_dir = None
    if args.project_mode == "new" and not args.project_id:
        resume_dir = find_resume_run(work_base, src)
    if resume_dir:
        out_dir = resume_dir
        print(f"[resume] continuing previous unfinished run: {out_dir}", flush=True)
    else:
        stamp   = time.strftime("%Y%m%d_%H%M%S")
        out_dir = work_base / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    title   = args.title or (src.stem + " Studio smooth mix")

    print("[auth] using persistent Suno login browser...", flush=True)
    require_project_keys = bool(args.project_id is None and args.project_mode == "current")
    login_url = "https://suno.com/studio" if require_project_keys else "https://suno.com/"
    ctx = None
    if (os.environ.get("SUNO_PREFER_SAVED_TOKEN", "1") == "1"
            and not require_project_keys):
        ctx = load_saved_login(DEFAULT_TOKEN_FILE,
                               max_age_sec=int(os.environ.get("SUNO_SAVED_TOKEN_MAX_AGE", "7200")))
        if ctx:
            print(f"[auth] using saved Suno login tokenSource={ctx.get('tokenSource')} "
                  f"href={ctx.get('href')}", flush=True)
    if not ctx:
        if os.environ.get("SUNO_NO_BROWSER_ON_UPLOAD", "0") == "1":
            raise RuntimeError(f"没有可用的 Suno 登录状态，请先点击[连接 Suno]，通过默认浏览器登录助手连接后再上传。token路径：{DEFAULT_TOKEN_FILE}")
        ctx = export_chrome_suno_token(DEFAULT_TOKEN_FILE, args.login_timeout,
                                       require_project_keys=require_project_keys,
                                       login_url=login_url)

    def refresh_suno_token() -> Dict[str, Any]:
        if os.environ.get("SUNO_NO_BROWSER_ON_UPLOAD", "0") == "1":
            source = str(ctx.get("tokenSource") or "")
            if source.startswith("fengling-extension"):
                refreshed = wait_for_refreshed_extension_login(
                    client.token,
                    timeout_sec=float(os.environ.get("SUNO_EXTENSION_REFRESH_WAIT", "25")),
                )
                if refreshed:
                    ctx.update(refreshed)
                    return refreshed
            elif cdp_alive():
                refreshed = export_chrome_suno_token(
                    DEFAULT_TOKEN_FILE, 30,
                    require_project_keys=False,
                    login_url="https://suno.com/",
                )
                if refreshed and refreshed.get("token") != client.token:
                    ctx.update(refreshed)
                    print("[auth-refresh] received fresh dedicated-browser token", flush=True)
                    return refreshed
            raise RuntimeError(
                "Suno 登录状态已失效，软件未能自动刷新登录状态。"
                "扩展登录请保持已登录 Suno 的浏览器页面打开；"
                "内置登录请保持专用浏览器打开，然后重试。"
            )
        return export_chrome_suno_token(
            DEFAULT_TOKEN_FILE, min(args.login_timeout, 180),
            require_project_keys=False,
            login_url="https://suno.com/",
        )

    client = SunoClient(ctx["token"], ctx.get("deviceId") or "", token_refresh=refresh_suno_token)
    client.api("GET", "/api/feed/v2?is_liked=false&limit=1&page=0", timeout=45)
    print("[auth] Suno login preflight verified", flush=True)

    if args.project_id:
        project_id = args.project_id
        print(f"[project] using specified Studio project id={project_id}", flush=True)
    elif args.project_mode == "current":
        project_id = project_id_from_context(ctx)
        print(f"[project] using current browser Studio project id={project_id}", flush=True)
    else:
        project_id = project_id_from_run_dir(out_dir) if resume_dir else None
        if project_id:
            print(f"[resume] using previous Studio project id={project_id}", flush=True)
        else:
            project_id = create_studio_project(client, title, out_dir)

    if not args.keep_studio_open and args.project_mode == "current":
        move_chrome_away()

    if resume_dir and (out_dir / "split_manifest.json").exists():
        segs, manifest = load_segments_from_manifest(out_dir)
        print(f"[resume] loaded split manifest with {len(segs)} base segments", flush=True)
    else:
        segs, manifest = split_audio(src, out_dir, args.target_sec, args.max_sec,
                                     args.search_sec, args.overlap_sec)
    initialize_clip    = args.clip_reference != "upload-id"
    if not initialize_clip:
        print("[mode] upload-id raw mode: skip initialize-clip", flush=True)
    progress_path = out_dir / "uploaded_rows.progress.json"
    rows = upload_all(
        client, segs, args.concurrency,
        args.retry_split_min_sec, args.overlap_sec,
        args.retry_max_depth, initialize_clip, progress_path,
    )
    write_json_atomic(out_dir / "uploaded_rows.json", rows)

    result = assemble_render(
        client, project_id, rows, title, out_dir,
        render=not args.no_render,
        clip_reference=args.clip_reference,
        track_count=args.tracks,
    )

    final = result.get("finalClip") or {}
    rid   = final.get("id") or (
        (result.get("render") or {}).get("id")
        if isinstance(result.get("render"), dict) else None
    )
    if rid:
        open_url_default_browser(f"https://suno.com/song/{rid}")
    else:
        open_url_default_browser("https://suno.com/studio")

    local_audio_path = None
    if rid and final.get("audio_url"):
        try:
            local_audio = out_dir / f"final_render_{rid}.mp3"
            rr = requests.get(final["audio_url"], timeout=180)
            rr.raise_for_status()
            local_audio.write_bytes(rr.content)
            local_audio_path = str(local_audio)
            print(f"[download] final mp3 -> {local_audio}", flush=True)
        except Exception as e:
            print(f"[download] final mp3 skipped: {e}", flush=True)

    summary = {
        "workDir":        str(out_dir),
        "projectId":      result["projectId"],
        "versionId":      result.get("versionId"),
        "clipCount":      result["clipCount"],
        "totalSeconds":   result["totalSeconds"],
        "renderId":       rid,
        "status":         final.get("status"),
        "audioUrl":       final.get("audio_url"),
        "localAudioPath": local_audio_path,
        "songUrl":        f"https://suno.com/song/{rid}" if rid else None,
    }
    write_json_atomic(out_dir / "SUCCESS_SUMMARY.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
