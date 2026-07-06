from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__


CONFIG_DIR = Path.home() / ".fengling"
CONFIG_PATH = CONFIG_DIR / "config.json"
DEFAULT_LOCAL_ROOT = Path.home() / "fengling-studio"


class CliError(RuntimeError):
    def __init__(self, message: str, *, code: str = "error", details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


@dataclass
class Config:
    app_root: str = str(DEFAULT_LOCAL_ROOT)
    python_path: str = ""

    @property
    def backend_rel(self) -> str:
        return "scripts/suno_auto_recut_upload.py"

    @property
    def log_rel(self) -> str:
        return "log"

    @property
    def runs_rel(self) -> str:
        return "derived/auto_recut_run"


def load_config() -> Config:
    data: dict[str, Any] = {}
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            raise CliError(f"Cannot parse config: {CONFIG_PATH}", code="config_parse", details={"error": str(exc)})
    return Config(
        app_root=str(os.environ.get("FENGLING_APP_ROOT") or data.get("app_root") or DEFAULT_LOCAL_ROOT),
        python_path=str(os.environ.get("FENGLING_PYTHON") or data.get("python_path") or ""),
    )


def save_config(cfg: Config) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg.__dict__, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def print_result(value: Any, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2))
    else:
        print_human(value)


def print_human(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                print(f"{key}: {json.dumps(item, ensure_ascii=False)}")
            else:
                print(f"{key}: {item}")
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                print("  ".join(f"{k}={v}" for k, v in item.items()))
            else:
                print(item)
    else:
        print(value)


def fail(exc: Exception, *, as_json: bool) -> int:
    if isinstance(exc, CliError):
        payload = {"ok": False, "error": {"code": exc.code, "message": str(exc), "details": exc.details}}
    else:
        payload = {"ok": False, "error": {"code": "unexpected", "message": str(exc), "details": {}}}
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
    else:
        print(f"error: {payload['error']['message']}", file=sys.stderr)
    return 1


def run_cmd(args: list[str], *, input_text: str | None = None, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )


def local_path(cfg: Config, *parts: str) -> Path:
    return Path(cfg.app_root).expanduser().joinpath(*parts)


def read_json_file(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise CliError(f"Cannot read JSON file: {path}", code="json_read_failed", details={"error": str(exc)})


def cmd_config_init(args: argparse.Namespace) -> dict[str, Any]:
    cfg = Config(app_root=args.app_root or str(DEFAULT_LOCAL_ROOT), python_path=args.python or "")
    save_config(cfg)
    return {"ok": True, "config_path": str(CONFIG_PATH), "config": redacted_config(cfg)}


def redacted_config(cfg: Config) -> dict[str, Any]:
    return {"app_root": cfg.app_root, "python_path": cfg.python_path or "(auto)"}


def cmd_config_show(args: argparse.Namespace) -> dict[str, Any]:
    cfg = load_config()
    return {"ok": True, "config_path": str(CONFIG_PATH), "config": redacted_config(cfg)}


def configured_python(cfg: Config) -> str:
    return cfg.python_path or shutil.which("python3") or "python3"


def doctor_local(cfg: Config) -> dict[str, Any]:
    root = Path(cfg.app_root).expanduser()
    py = Path(configured_python(cfg))
    backend = root / cfg.backend_rel
    log_dir = root / cfg.log_rel
    runs_dir = root / cfg.runs_rel
    chrome_candidates = [
        Path("/Applications/Google Chrome.app"),
        Path.home() / "Applications" / "Google Chrome.app",
        Path("/Applications/Microsoft Edge.app"),
        Path.home() / "Applications" / "Microsoft Edge.app",
        Path("/Applications/Chromium.app"),
        Path.home() / "Applications" / "Chromium.app",
    ]
    browser_apps = [str(p) for p in chrome_candidates if p.exists()]
    browser_bins = [p for p in ("google-chrome", "chrome", "chromium", "microsoft-edge") if shutil.which(p)]
    ffmpeg = shutil.which("ffmpeg") or str(root / "bin" / "ffmpeg")
    ffmpeg_exists = bool(shutil.which("ffmpeg") or (root / "bin" / "ffmpeg").exists())
    opencli = shutil.which("opencli") or ""
    opencli_doctor = check_opencli_doctor(opencli)
    deps = check_python_deps(py if py else Path(sys.executable))
    return {
        "mode": "local",
        "config_path": str(CONFIG_PATH),
        "app_root": str(root),
        "app_root_exists": root.exists(),
        "python": str(py) if py else "",
        "python_exists": py.exists() if py else False,
        "backend": str(backend),
        "backend_exists": backend.exists(),
        "log_dir_exists": log_dir.exists(),
        "runs_dir_exists": runs_dir.exists(),
        "ffmpeg": ffmpeg,
        "ffmpeg_exists": ffmpeg_exists,
        "opencli": opencli,
        "opencli_exists": bool(opencli),
        "opencli_doctor_ok": opencli_doctor["ok"],
        "opencli_doctor_error": opencli_doctor["error"],
        "browser_apps": browser_apps,
        "browser_bins": browser_bins,
        "browser_available": bool(browser_apps or browser_bins),
        "python_deps": deps,
    }


def check_python_deps(py: Path) -> dict[str, bool]:
    if not py:
        return {"numpy": False, "requests": False, "websocket": False}
    code = "import importlib.util,json; print(json.dumps({m: importlib.util.find_spec(m) is not None for m in ['numpy','requests','websocket']}))"
    cp = run_cmd([str(py), "-c", code], timeout=20)
    if cp.returncode != 0:
        return {"numpy": False, "requests": False, "websocket": False}
    try:
        return json.loads(cp.stdout.strip())
    except Exception:
        return {"numpy": False, "requests": False, "websocket": False}


def check_opencli_doctor(opencli: str) -> dict[str, Any]:
    if not opencli:
        return {"ok": False, "error": "opencli not found"}
    cp = run_cmd([opencli, "doctor"], timeout=30)
    if cp.returncode == 0:
        return {"ok": True, "error": ""}
    return {"ok": False, "error": (cp.stderr or cp.stdout)[-500:]}


def cmd_doctor(args: argparse.Namespace) -> dict[str, Any]:
    cfg = load_config()
    data = doctor_local(cfg)
    missing = []
    for key in (
        "app_root_exists", "python_exists", "backend_exists", "ffmpeg_exists",
        "browser_available", "opencli_exists", "opencli_doctor_ok",
    ):
        if not data.get(key):
            missing.append(key)
    deps = data.get("python_deps") or {}
    for dep, present in deps.items():
        if not present:
            missing.append(f"python_dep:{dep}")
    data["ok"] = not missing
    data["missing"] = missing
    data["version"] = __version__
    return data


def cmd_runs_list(args: argparse.Namespace) -> dict[str, Any]:
    cfg = load_config()
    base = local_path(cfg, "derived", "auto_recut_run")
    items = []
    if base.exists():
        dirs = sorted([p for p in base.iterdir() if p.is_dir()], key=lambda p: p.stat().st_mtime, reverse=True)
        for p in dirs[: args.limit]:
            summary = p / "SUCCESS_SUMMARY.json"
            item: dict[str, Any] = {"id": p.name, "path": str(p), "modified": p.stat().st_mtime, "success": summary.exists()}
            if summary.exists():
                item.update(read_json_file(summary))
            items.append(item)
    return {"ok": True, "runs": items, "count": len(items)}


def cmd_run_get(args: argparse.Namespace) -> dict[str, Any]:
    cfg = load_config()
    run_dir = local_path(cfg, "derived", "auto_recut_run", args.run_id)
    if not run_dir.exists():
        raise CliError("Run not found", code="run_not_found", details={"run_id": args.run_id})
    files = [{"name": p.name, "size": p.stat().st_size} for p in sorted(run_dir.iterdir()) if p.is_file()]
    run: dict[str, Any] = {"id": args.run_id, "path": str(run_dir), "files": files}
    summary = run_dir / "SUCCESS_SUMMARY.json"
    if summary.exists():
        run["summary"] = read_json_file(summary)
    return {"ok": True, "run": run}


def cmd_logs_list(args: argparse.Namespace) -> dict[str, Any]:
    cfg = load_config()
    log_dir = local_path(cfg, "log")
    logs = []
    if log_dir.exists():
        files = sorted([p for p in log_dir.iterdir() if p.is_file()], key=lambda p: p.stat().st_mtime, reverse=True)
        logs = [{"name": p.name, "path": str(p), "size": p.stat().st_size} for p in files[: args.limit]]
    return {"ok": True, "logs": logs, "count": len(logs)}


def cmd_logs_tail(args: argparse.Namespace) -> dict[str, Any]:
    cfg = load_config()
    log_dir = local_path(cfg, "log")
    files = sorted([p for p in log_dir.glob(args.name or "*") if p.is_file()], key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        raise CliError("Log not found", code="log_not_found")
    p = files[0]
    payload = {"name": p.name, "path": str(p), "lines": p.read_text(encoding="utf-8", errors="replace").splitlines()[-args.lines:]}
    return {"ok": True, "log": payload}


def build_backend_args(args: argparse.Namespace) -> list[str]:
    backend_args = [args.source]
    opt_map = [
        ("--title", args.title),
        ("--work-dir", args.work_dir),
        ("--target-sec", args.target_sec),
        ("--max-sec", args.max_sec),
        ("--search-sec", args.search_sec),
        ("--overlap-sec", args.overlap_sec),
        ("--concurrency", args.concurrency),
        ("--retry-split-min-sec", args.retry_split_min_sec),
        ("--retry-max-depth", args.retry_max_depth),
        ("--clip-reference", args.clip_reference),
        ("--tracks", args.tracks),
        ("--project-mode", args.project_mode),
        ("--project-id", args.project_id),
        ("--login-timeout", args.login_timeout),
    ]
    for flag, value in opt_map:
        if value is not None:
            backend_args.extend([flag, str(value)])
    if args.no_render:
        backend_args.append("--no-render")
    if args.keep_studio_open:
        backend_args.append("--keep-studio-open")
    return backend_args


def backend_command(cfg: Config, backend_args: list[str]) -> list[str]:
    py = configured_python(cfg)
    return [py, str(local_path(cfg, "scripts", "suno_auto_recut_upload.py"))] + backend_args


def backend_env(cfg: Config) -> dict[str, str]:
    env = os.environ.copy()
    env["SUNO_APP_ROOT"] = cfg.app_root
    env["SUNO_USE_CDP_BROWSER"] = "1"
    env["SUNO_DEFAULT_BROWSER_ONLY"] = "0"
    env.setdefault("SUNO_USE_OPENCLI_BROWSER", "1")
    env.setdefault("SUNO_OPENCLI_SESSION", "fengling-suno")
    return env


def cmd_run_upload(args: argparse.Namespace) -> dict[str, Any]:
    cfg = load_config()
    cmd = backend_command(cfg, build_backend_args(args))
    payload = {"ok": True, "mode": "local", "dry_run": not args.execute, "command": cmd}
    if not args.execute:
        return payload
    cp = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=backend_env(cfg))
    payload["returncode"] = cp.returncode
    payload["stdout_tail"] = cp.stdout[-8000:]
    payload["stderr_tail"] = cp.stderr[-8000:]
    if cp.returncode != 0:
        raise CliError("Upload command failed", code="upload_failed", details=payload)
    payload["summary"] = parse_summary_from_stdout(cp.stdout)
    return payload


def parse_summary_from_stdout(text: str) -> dict[str, Any] | None:
    candidates = re.findall(r"\{(?:.|\n)*?\}", text)
    for candidate in reversed(candidates):
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict) and ("workDir" in parsed or "renderId" in parsed):
            return parsed
    return None


def cmd_browser_preheat(args: argparse.Namespace) -> dict[str, Any]:
    cfg = load_config()
    cmd = backend_command(cfg, ["--preheat-browser"])
    payload = {"ok": True, "mode": "local", "dry_run": not args.execute, "command": cmd}
    if not args.execute:
        return payload
    cp = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=backend_env(cfg), timeout=60)
    payload["returncode"] = cp.returncode
    payload["stdout_tail"] = cp.stdout[-4000:]
    payload["stderr_tail"] = cp.stderr[-4000:]
    if cp.returncode != 0:
        raise CliError("Browser preheat failed", code="browser_preheat_failed", details=payload)
    return payload


def cmd_deps_install(args: argparse.Namespace) -> dict[str, Any]:
    cfg = load_config()
    packages = ["numpy", "requests", "websocket-client"]
    cmd = [configured_python(cfg), "-m", "pip", "install"] + packages
    payload = {"ok": True, "dry_run": not args.execute, "command": cmd, "packages": packages}
    if not args.execute:
        return payload
    cp = run_cmd(cmd, timeout=600)
    payload["returncode"] = cp.returncode
    payload["stdout_tail"] = cp.stdout[-4000:]
    payload["stderr_tail"] = cp.stderr[-4000:]
    if cp.returncode != 0:
        raise CliError("Dependency install failed", code="deps_install_failed", details=payload)
    return payload


def cmd_raw_script(args: argparse.Namespace) -> dict[str, Any]:
    cfg = load_config()
    backend_args = list(args.args)
    if backend_args and backend_args[0] == "--":
        backend_args = backend_args[1:]
    cmd = backend_command(cfg, backend_args)
    payload = {"ok": True, "mode": "local", "dry_run": not args.execute, "command": cmd}
    if not args.execute:
        return payload
    cp = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=backend_env(cfg))
    payload["returncode"] = cp.returncode
    payload["stdout_tail"] = cp.stdout[-8000:]
    payload["stderr_tail"] = cp.stderr[-8000:]
    if cp.returncode != 0:
        raise CliError("Raw script command failed", code="raw_failed", details=payload)
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fengling", description="Mac CLI for Fengling / Suno recut automation.")
    parser.add_argument("--json", action="store_true", help="Emit stable JSON.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="Check config, runtime, script paths, and local dependencies.").set_defaults(func=cmd_doctor)

    config = sub.add_parser("config", help="Manage ~/.fengling/config.json.")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    init = config_sub.add_parser("init", help="Write local CLI config.")
    init.add_argument("--app-root", default=str(DEFAULT_LOCAL_ROOT))
    init.add_argument("--python")
    init.set_defaults(func=cmd_config_init)
    config_sub.add_parser("show", help="Show current config.").set_defaults(func=cmd_config_show)

    runs = sub.add_parser("runs", help="Read generated run directories.")
    runs_sub = runs.add_subparsers(dest="runs_command", required=True)
    runs_list = runs_sub.add_parser("list", help="List recent runs.")
    runs_list.add_argument("--limit", type=int, default=10)
    runs_list.set_defaults(func=cmd_runs_list)
    run_get = runs_sub.add_parser("get", help="Read one run by directory id.")
    run_get.add_argument("run_id")
    run_get.set_defaults(func=cmd_run_get)

    logs = sub.add_parser("logs", help="Read upload logs.")
    logs_sub = logs.add_subparsers(dest="logs_command", required=True)
    logs_list = logs_sub.add_parser("list", help="List recent log files.")
    logs_list.add_argument("--limit", type=int, default=10)
    logs_list.set_defaults(func=cmd_logs_list)
    logs_tail = logs_sub.add_parser("tail", help="Tail the newest log, or newest matching name pattern.")
    logs_tail.add_argument("--name", help="Glob pattern such as upload_20260706*.log")
    logs_tail.add_argument("--lines", type=int, default=40)
    logs_tail.set_defaults(func=cmd_logs_tail)

    browser = sub.add_parser("browser", help="Browser/login helper commands.")
    browser_sub = browser.add_subparsers(dest="browser_command", required=True)
    preheat = browser_sub.add_parser("preheat", help="Preheat the dedicated login browser.")
    preheat.add_argument("--execute", action="store_true", help="Actually run the helper. Without this, preview only.")
    preheat.set_defaults(func=cmd_browser_preheat)

    deps = sub.add_parser("deps", help="Manage local Python dependencies.")
    deps_sub = deps.add_subparsers(dest="deps_command", required=True)
    deps_install = deps_sub.add_parser("install", help="Install numpy, requests, and websocket-client for the configured Python.")
    deps_install.add_argument("--execute", action="store_true", help="Actually install. Without this, preview only.")
    deps_install.set_defaults(func=cmd_deps_install)

    run = sub.add_parser("run", help="Run automation workflows.")
    run_sub = run.add_subparsers(dest="run_command", required=True)
    upload = run_sub.add_parser("upload", help="Split, upload, assemble, and optionally render an audio file.")
    upload.add_argument("source")
    upload.add_argument("--title")
    upload.add_argument("--work-dir")
    upload.add_argument("--target-sec", type=float)
    upload.add_argument("--max-sec", type=float)
    upload.add_argument("--search-sec", type=float)
    upload.add_argument("--overlap-sec", type=float)
    upload.add_argument("--concurrency", type=int)
    upload.add_argument("--retry-split-min-sec", type=float)
    upload.add_argument("--retry-max-depth", type=int)
    upload.add_argument("--clip-reference", choices=["clip-id", "upload-id"])
    upload.add_argument("--tracks", type=int)
    upload.add_argument("--project-mode", choices=["new", "current"])
    upload.add_argument("--project-id")
    upload.add_argument("--login-timeout", type=int)
    upload.add_argument("--no-render", action="store_true")
    upload.add_argument("--keep-studio-open", action="store_true")
    upload.add_argument("--execute", action="store_true", help="Actually run upload. Without this, preview only.")
    upload.set_defaults(func=cmd_run_upload)

    raw = sub.add_parser("raw", help="Raw escape hatches.")
    raw_sub = raw.add_subparsers(dest="raw_command", required=True)
    script = raw_sub.add_parser("script", help="Pass arguments directly to suno_auto_recut_upload.py.")
    script.add_argument("--execute", action="store_true", help="Actually run. Without this, preview only.")
    script.add_argument("args", nargs=argparse.REMAINDER)
    script.set_defaults(func=cmd_raw_script)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        payload = args.func(args)
        print_result(payload, as_json=args.json)
        return 0
    except Exception as exc:
        return fail(exc, as_json=args.json)


if __name__ == "__main__":
    raise SystemExit(main())
